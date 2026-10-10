"""Text style analysis (Task 9): the fill colour, fill gradient, alpha fade and effects (stroke, shadow, glow) of a
text element, measured on its matted texture and the local plate (never the global background), plus the glyph
measures font matching starts from (stroke width, cap height, stroke ratio, glyph centres).

Effects are fitted with the renderer's own forward model (`fonts.raster.compose_text`) over a crop wider than the
texture, so the analysed style redraws what the reference shows."""
from __future__ import annotations
import math
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable
import cv2
import numpy as np
from ..fonts.raster import _blur, _dilate, _shift, compose_text
from ..ir.colour import delta_e, hex_to_rgb8, lab_to_srgb, rgb8_to_hex, srgb_to_lab
from ..ir.gradient import gradient_t, render_gradient
from ..ir.schema import AlphaStop, Fade, Gradient, GradientStop, TextEffect, TextStyle, TextureMeta
from ..log import describe, get
from . import matting

log = get("keepframe.analyze.textstyle")

STYLE_FAILED = "style_failed"   # `style_error` of a text whose style analysis failed (a code: what clients see)

INTERIOR_K = 0.35            # fill interior = α > 0.5 eroded by this × stroke width …
THIN_PX = 3.0                # … unless strokes are thinner: then the most opaque decile
SOLID_A = 0.5
INK_A = 0.05                 # α above this is glyph; effects are measured outside it
GRADIENT_DROP = 0.40         # a linear fill gradient must cut the interior SSE by this …
GRADIENT_DE = 6.0            # … and its stops differ by at least this ΔE76
GROW_DE = 12.0               # interior pixels within this ΔE76 of the fitted ramp join it (rounds below) …
GROW_ROUNDS = 3              # … so a strong ramp is followed across the fill, not extrapolated from one band
FADE_RANGE = 0.3             # interior α range (p95 − p5) above which a fade is tried …
FADE_DROP = 0.40             # … and kept when a linear α ramp cuts the residual by this
EFFECT_DROP = 0.30           # an effect must cut the residual energy outside the glyphs by this
SHADOW_REACH = 0.3           # shadow offsets within ± this × cap height
SHADOW_SIGMAS = (0, 1, 2, 4, 8)
GLOW_SIGMAS = (2, 4, 8, 16)
STROKE_WIDTHS = (1, 2, 3, 4, 6, 8)
STROKE_MAX = 0.15            # × cap height
RING_SPREAD = 8.0            # ΔE76 spread of a stroke ring's colour (p75 from its median)
EFFECT_FILL_DE = 10.0        # an effect in the fill's own colour is a heavier weight or a misregistration, not an effect
FG_DE = 12.0                 # texture pixels this close to the local plate are plate, not glyph (matting's threshold)
THICK_K = 0.75               # fill pixels deeper than this × stroke width inside the solid mask are a blob, not glyph
BOX_SHARE = 0.95             # a texture opaque over this share of its box …
KEPT_PLATE_DE = 30.0         # … whose border is this close to the plate kept the plate: glyphs by contrast
SIM_DE = (10.0, 20.0)        # texture colour within the first ΔE of the fill is fill, beyond the second it is not
MAJORITY = 0.5               # a fill colour on less than this share of the stroke centres yields to one on more
CORE_DE = 20.0               # a fill this far from the core-mask colour may be the surroundings the matte kept …
CORE_SHARE = 0.10            # … when this share of the solid pixels is within SIM_DE[1] of it (the glyphs)
UNFILLED = 0.2               # stroke centres the fill does not draw over this share: a second colour (confidence × 0.8)
EFFECT_FRAMES = 4            # frames averaged for the effect crop
CAP_WORK = 48.0              # effects are searched at a scale where the cap height is at most this
OPACITIES = np.round(np.arange(0.05, 1.0001, 0.05), 2)
_K3 = np.ones((3, 3), np.uint8)


# --- glyph measures -----------------------------------------------------------------------------------------------

def _a01(alpha) -> np.ndarray:
    a = np.asarray(alpha, np.float32)
    return a / 255.0 if a.size and a.max() > 1.5 else a


def stroke_width(alpha) -> float:
    """Stroke (stem) width in px: 2 × the median distance-transform value on ridge pixels of α > 0.5, measured on a
    bilinear upsample (sub-pixel edges). The texture border counts as outside."""
    a = _a01(alpha)
    h, w = a.shape
    s = int(np.clip(math.ceil(360.0 / max(1, h)), 2, 8))
    while s > 1 and h * w * s * s > 6_000_000:
        s -= 1
    up = cv2.resize(a, (w * s, h * s), interpolation=cv2.INTER_LINEAR) if s > 1 else a
    m = np.pad(up > SOLID_A, 1)
    if not m.any():
        return 0.0
    dt = cv2.distanceTransform(m.astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)[1:-1, 1:-1]
    ridge = (dt > 0) & (dt >= cv2.dilate(dt, _K3) - 1e-4)
    v = dt[ridge]
    return float(max(0.0, (2.0 * np.median(v) - 0.5) / s)) if v.size else 0.0


def _components(a: np.ndarray, thr: float):
    n, lab, stats, _ = cv2.connectedComponentsWithStats((a > thr).astype(np.uint8), connectivity=8)
    if n <= 1:
        return lab, np.zeros((0, 5), np.int64), np.zeros(0, np.int64)
    st = stats[1:]
    keep = st[:, cv2.CC_STAT_AREA] >= max(2, 0.01 * st[:, cv2.CC_STAT_AREA].max())
    return lab, st[keep], np.flatnonzero(keep) + 1


def _line_groups(st: np.ndarray, k: int) -> list[np.ndarray]:
    """Indices of components per text line: split the vertical centres at the k − 1 widest gaps."""
    if k <= 1 or len(st) < k:
        return [np.arange(len(st))]
    cy = st[:, cv2.CC_STAT_TOP] + st[:, cv2.CC_STAT_HEIGHT] / 2
    order = np.argsort(cy)
    gaps = np.diff(cy[order])
    cuts = np.sort(np.argsort(gaps)[-(k - 1):]) + 1
    return [order[s] for s in np.split(np.arange(len(order)), cuts)]


def cap_height(alpha, lines: int = 1) -> float:
    """Cap height in px: per line, the baseline is the median component bottom; the 90th percentile height above it of
    the components sitting on it (descenders, quotes and dots left out). Median over lines."""
    a = _a01(alpha)
    _, st, _ = _components(a, SOLID_A)
    if not len(st):
        return 0.0
    out = []
    for g in _line_groups(st, max(1, lines)):
        top, hh = st[g, cv2.CC_STAT_TOP].astype(np.float64), st[g, cv2.CC_STAT_HEIGHT].astype(np.float64)
        bottom = top + hh
        base = float(np.median(bottom))
        on = np.abs(bottom - base) <= max(1.5, 0.08 * hh.max())
        heights = base - top[on] if on.any() else hh
        out.append(float(np.percentile(heights, 90)))
    return float(np.median(out))


def glyph_centres(alpha, text: str) -> list[float] | None:
    """α-weighted x centre of every glyph (spaces skipped), in reading order; lines top to bottom. Components that
    overlap in x (a dot and its stem, jamo of one syllable) form one glyph; extra pieces merge across the smallest
    gaps. None when the glyphs cannot be told apart (fewer pieces than characters)."""
    a = _a01(alpha)
    lines = [ln for ln in text.split("\n") if "".join(ln.split())]
    want = [len("".join(ln.split())) for ln in lines]
    if not want:
        return None
    lab, st, ids = _components(a, 0.35)
    if len(st) < sum(want):
        return None
    out: list[float] = []
    for g, n in zip(_line_groups(st, len(lines)), want):
        g = g[np.argsort(st[g, cv2.CC_STAT_LEFT])]
        groups: list[list[int]] = []
        spans: list[list[float]] = []
        for i in g:
            x0 = float(st[i, cv2.CC_STAT_LEFT])
            x1 = x0 + float(st[i, cv2.CC_STAT_WIDTH])
            if spans:
                s0, s1 = spans[-1]
                if min(s1, x1) - max(s0, x0) >= 0.5 * min(x1 - x0, s1 - s0):
                    groups[-1].append(int(i))
                    spans[-1] = [min(s0, x0), max(s1, x1)]
                    continue
            groups.append([int(i)])
            spans.append([x0, x1])
        while len(groups) > n:
            j = int(np.argmin([spans[k + 1][0] - spans[k][1] for k in range(len(spans) - 1)]))
            groups[j] += groups.pop(j + 1)
            spans[j] = [min(spans[j][0], spans[j + 1][0]), max(spans[j][1], spans.pop(j + 1)[1])]
        if len(groups) < n:
            return None
        xs = np.arange(a.shape[1], dtype=np.float64) + 0.5
        for grp in groups:
            m = np.isin(lab, ids[grp])
            wts = (a * m).sum(0)
            out.append(round(float((wts * xs).sum() / max(wts.sum(), 1e-9)), 3))
    return out


# --- fill, gradient, fade -----------------------------------------------------------------------------------------

def _disc(r: int) -> np.ndarray:
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))


def interior_mask(alpha, stroke_w: float) -> np.ndarray:
    """α > 0.5 eroded by 0.35 × stroke width; strokes thinner than 3 px (or nothing left): the most opaque decile."""
    a = _a01(alpha)
    if stroke_w >= THIN_PX:
        r = max(1, int(round(INTERIOR_K * stroke_w)))
        m = cv2.erode((a > SOLID_A).astype(np.uint8), _disc(r), borderType=cv2.BORDER_CONSTANT, borderValue=0) > 0
        if m.sum() >= 4:
            return m
    v = a[a > INK_A]
    return (a >= np.percentile(v, 90)) & (a > INK_A) if v.size else np.zeros(a.shape, bool)


def stroke_centres(alpha, stroke_w: float) -> np.ndarray:
    """The strokes' centre lines: ridge pixels of the depth inside α > 0.5, in the fill interior and no deeper than
    0.75 × stroke width. A blob baked into the texture has its ridge deeper, so the rim of it the interior keeps has
    none: a colour's share of these pixels is its share of the glyph area."""
    a = _a01(alpha)
    dt = cv2.distanceTransform(np.pad(a > SOLID_A, 1).astype(np.uint8), cv2.DIST_L2, 3)[1:-1, 1:-1]
    return interior_mask(a, stroke_w) & (dt <= max(2.0, THICK_K * stroke_w)) & (dt >= cv2.dilate(dt, _K3) - 1e-4)


def _mode(lab: np.ndarray) -> np.ndarray:
    """The dominant colour: the densest sample (most neighbours within ΔE 8), then the median of the pixels within
    ΔE 15 of it (three rounds). A minority of another colour (a blob baked into the texture) does not pull it."""
    sub = np.asarray(lab[:: max(1, len(lab) // 800)], np.float32)
    n2 = (sub * sub).sum(1)
    d2 = n2[:, None] + n2[None, :] - 2.0 * (sub @ sub.T)
    c = sub[int(np.argmax((d2 < 64.0).sum(1)))]
    for _ in range(3):
        near = delta_e(lab, c) < 15.0
        if not near.any():
            break
        c = np.median(lab[near], axis=0)
    return np.asarray(c, np.float32)


def fill_colour(rgba, stroke_w: float, plate_rgb=None) -> tuple[str, np.ndarray, float]:
    """The fill: the dominant Lab colour of the matted foreground over the glyph interior (α > 0.5 eroded by 0.35 ×
    stroke width), left out: pixels deep inside thick blobs and, given the local plate, pixels that are plate
    (ΔE < 12). A line in two colours (a word in another): the density peak may be the minority (a flat colour beats
    a ramp), so a colour on less than half the stroke centres yields to the mode of the rest when that is on more
    than half. Returns (hex, Lab, spread = p90 ΔE76 of the interior from it)."""
    rgba = np.asarray(rgba)
    a = rgba[..., 3].astype(np.float32) / 255.0
    F = rgba[..., :3].astype(np.float32)
    m = interior_mask(a, stroke_w)
    if not m.any():
        raise ValueError("no glyph interior")
    dt = cv2.distanceTransform(np.pad(a > SOLID_A, 1).astype(np.uint8), cv2.DIST_L2, 3)[1:-1, 1:-1]
    thin = m & (dt <= max(2.0, THICK_K * stroke_w))
    if thin.sum() >= 20:
        m = thin
    if plate_rgb is not None:
        fg = m & (delta_e(srgb_to_lab(F), srgb_to_lab(np.asarray(plate_rgb, np.float32))) >= FG_DE)
        if fg.sum() >= max(20, 0.2 * m.sum()):
            m = fg
    lab = srgb_to_lab(F[m])
    c = _mode(lab)
    cen = srgb_to_lab(F[m & stroke_centres(a, stroke_w)])
    if len(cen) >= 20 and float((delta_e(cen, c) < SIM_DE[1]).mean()) < MAJORITY:   # glyph area, not density
        rest = lab[delta_e(lab, c) >= SIM_DE[1]]
        c2 = _mode(rest) if len(rest) >= 20 else c
        if float((delta_e(cen, c2) < SIM_DE[1]).mean()) > MAJORITY:
            c = c2
    spread = float(np.percentile(delta_e(lab, c), 90))
    return rgb8_to_hex(lab_to_srgb(c)), c, spread


def contrast_glyphs(F: np.ndarray, behind) -> np.ndarray:
    """Glyph pixels of a texture that kept its box (the plate came with it): contrast against what is behind well
    above the box border's (the border is plate; the plate estimate may itself be off), else the top decile —
    today's core-mask rule, on the local plate."""
    de = delta_e(srgb_to_lab(np.asarray(F, np.float32)), srgb_to_lab(np.asarray(behind, np.float32)))
    border = np.concatenate([de[0], de[-1], de[:, 0], de[:, -1]])
    base = float(np.median(border))
    glyph = de > base + max(FG_DE, 4.5 * float(np.median(np.abs(border - base))))
    return glyph if glyph.sum() >= 20 else de >= np.percentile(de, 90)


def core_fill(F, alpha, fill_lab, core_lab, plate=None) -> np.ndarray | None:
    """The fill (Lab) when the one measured is the text's surroundings, which the matte kept (a dark box, a white
    note, a pink globe in a glyph; the plate estimate missed them). The core-mask colour (the most contrasting decile
    against the background; no sole arbiter: it can be the plate, or a shape kept in the texture) breaks the tie
    between the two polarities, on the texture's own evidence: a fill over ΔE 20 from it yields to the dominant
    colour of the solid pixels within ΔE 20 of it when those are 10 % of the solid pixels or more, and, given the
    local plate (N×3, its known pixels under the box), the core stands out from it by ΔE 20 more than the fill does
    (surroundings are background-like; a missed shape in the core's colour behind the glyphs is not). None
    otherwise."""
    if float(delta_e(fill_lab, core_lab)) <= CORE_DE:
        return None
    if plate is not None and len(plate) >= 20:
        pl = srgb_to_lab(np.asarray(plate, np.float32))
        if float(np.median(delta_e(pl, core_lab)) - np.median(delta_e(pl, fill_lab))) < CORE_DE:
            return None
    lab = srgb_to_lab(np.asarray(F, np.float32)[_a01(alpha) > SOLID_A])
    near = lab[delta_e(lab, core_lab) < SIM_DE[1]]
    return _mode(near) if len(near) >= max(20, CORE_SHARE * len(lab)) else None


def _xy(mask: np.ndarray):
    ys, xs = np.nonzero(mask)
    return xs.astype(np.float64) + 0.5, ys.astype(np.float64) + 0.5


def _ramp(values: np.ndarray, mask: np.ndarray, drop: float):
    """Fit values ≈ a + bx·x + by·y; when it cuts the SSE by `drop`, refit along the CSS line of the steepest
    direction: (angle, value at t=0, value at t=1, t of the samples). None otherwise."""
    h, w = mask.shape
    x, y = _xy(mask)
    v = values.reshape(len(x), -1).astype(np.float64)
    sse0 = float(((v - v.mean(0)) ** 2).sum())
    if len(x) < 30 or sse0 <= 1e-9:
        return None
    X = np.stack([np.ones_like(x), x - w / 2, y - h / 2], 1)
    coef, *_ = np.linalg.lstsq(X, v, rcond=None)
    if float(((v - X @ coef) ** 2).sum()) > (1 - drop) * sse0:
        return None
    grad = coef[1:3].T                                   # channels × (d/dx, d/dy)
    d = np.linalg.svd(grad)[2][0] if grad.shape[0] > 1 else grad[0] / max(np.linalg.norm(grad[0]), 1e-12)
    if float(np.sum(grad @ d)) < 0:                     # t grows with the values (sum over channels)
        d = -d
    angle = math.degrees(math.atan2(d[0], -d[1])) % 360.0
    g = Gradient(kind="linear", angle=angle, stops=[GradientStop(offset=0, color="#000000"), GradientStop(offset=1, color="#000000")])
    t = gradient_t(g, w, h, (x, y))
    T = np.stack([np.ones_like(t), t], 1)
    c, *_ = np.linalg.lstsq(T, v, rcond=None)
    if float(((v - T @ c) ** 2).sum()) > (1 - drop) * sse0:
        return None
    return angle, c[0], c[0] + c[1], t


def _srgb_ramp(F: np.ndarray, mask: np.ndarray, angle: float) -> tuple[float, np.ndarray, np.ndarray]:
    """Direction (sRGB plane fit, the sign kept toward `angle`) and the stops at t = 0 and 1 of a ramp over the box,
    fitted in sRGB along the CSS t: the renderer interpolates gradients in sRGB, so this is its exact model."""
    h, w = mask.shape
    x, y = _xy(mask)
    v = F[mask].astype(np.float64)
    X = np.stack([np.ones_like(x), x - w / 2, y - h / 2], 1)
    slope = np.linalg.lstsq(X, v, rcond=None)[0][1:3].T              # channels × (d/dx, d/dy)
    if np.linalg.norm(slope) > 1e-9:
        d = np.linalg.svd(slope)[2][0]
        th = math.radians(angle)
        if d[0] * math.sin(th) - d[1] * math.cos(th) < 0:
            d = -d
        angle = math.degrees(math.atan2(d[0], -d[1])) % 360.0
    g = Gradient(kind="linear", angle=angle, stops=[GradientStop(offset=0, color="#000000"), GradientStop(offset=1, color="#000000")])
    t = gradient_t(g, w, h, (x, y))
    c = np.linalg.lstsq(np.stack([np.ones_like(t), t], 1), v, rcond=None)[0]
    return angle, c[0], c[0] + c[1]


def _ramp_at(mask: np.ndarray, angle: float, c0: np.ndarray, c1: np.ndarray) -> np.ndarray:
    h, w = mask.shape
    g = Gradient(kind="linear", angle=angle, stops=[GradientStop(offset=0, color="#000000"), GradientStop(offset=1, color="#000000")])
    t = np.clip(gradient_t(g, w, h, _xy(mask)), 0.0, 1.0)[:, None]
    return np.clip(c0 + (c1 - c0) * t, 0.0, 255.0).astype(np.float32)


def fit_fill_gradient(F, interior, candidates=None) -> Gradient | None:
    """A two-stop linear gradient over the texture box. Detected on `interior` (Lab ≈ a + Bx·x + By·y cutting the SSE
    by > 40 %); then followed: pixels of `candidates` (default: the interior) within ΔE 12 of the ramp join and the
    direction and stops are refitted in sRGB (3 rounds), so a strong ramp is fitted across the whole fill rather
    than extrapolated from the band it was found in. Kept when the final ramp still cuts the SSE by > 40 % and its
    stops differ by ΔE ≥ 6; None for a flat fill."""
    interior = np.asarray(interior, bool)
    F = np.asarray(F, np.float32)
    if interior.sum() < 30:
        return None
    fit = _ramp(srgb_to_lab(F[interior]), interior, GRADIENT_DROP)
    if fit is None:
        return None
    cand = interior if candidates is None else np.asarray(candidates, bool) | interior
    angle, inl = fit[0], interior
    for _ in range(GROW_ROUNDS):
        angle, c0, c1 = _srgb_ramp(F, inl, angle)
        grown = np.zeros_like(cand)
        grown[cand] = delta_e(srgb_to_lab(F[cand]), srgb_to_lab(_ramp_at(cand, angle, c0, c1))) < GROW_DE
        if grown.sum() < 30 or np.array_equal(grown, inl):
            break
        inl = grown
    angle, c0, c1 = _srgb_ramp(F, inl, angle)
    lab = srgb_to_lab(F[inl])
    sse0 = float(((lab - lab.mean(0)) ** 2).sum())
    sse1 = float((delta_e(lab, srgb_to_lab(_ramp_at(inl, angle, c0, c1))) ** 2).sum())
    s0, s1 = np.clip(np.rint(c0), 0, 255), np.clip(np.rint(c1), 0, 255)
    if sse0 <= 1e-9 or sse1 > (1 - GRADIENT_DROP) * sse0 or float(delta_e(srgb_to_lab(s0), srgb_to_lab(s1))) < GRADIENT_DE:
        return None
    return Gradient(kind="linear", angle=round(angle, 2),
                    stops=[GradientStop(offset=0.0, color=rgb8_to_hex(s0)), GradientStop(offset=1.0, color=rgb8_to_hex(s1))])


def fit_fade(alpha, interior) -> Fade | None:
    """An alpha ramp over the box when the interior α spans > 0.3 and a linear ramp cuts its residual by > 40 %.
    Stops at both ends and where the ramp crosses 0 or 1 (2–4 stops)."""
    a = _a01(alpha)
    interior = np.asarray(interior, bool)
    v = a[interior]
    if v.size < 30 or float(np.percentile(v, 95) - np.percentile(v, 5)) <= FADE_RANGE:
        return None
    fit = _ramp(v, interior, FADE_DROP)
    if fit is None:
        return None
    angle, a0, a1 = fit[0], float(fit[1][0]), float(fit[2][0])
    ts = {0.0, 1.0}
    if abs(a1 - a0) > 1e-6:
        for level in (0.0, 1.0):
            t = (level - a0) / (a1 - a0)
            if 0.0 < t < 1.0:
                ts.add(round(t, 4))
    stops = [AlphaStop(offset=t, alpha=round(float(np.clip(a0 + (a1 - a0) * t, 0.0, 1.0)), 4)) for t in sorted(ts)]
    return Fade(angle=round(angle, 2), stops=stops)


def fade_map(fade: Fade, w: int, h: int, pad: int = 0) -> np.ndarray:
    """The fade over a w×h box plus `pad` px a side (box geometry, as `raster.fade_alpha` draws it)."""
    g = Gradient(kind="linear", angle=fade.angle, stops=[GradientStop(offset=0, color="#000000"), GradientStop(offset=1, color="#000000")])
    ys, xs = np.mgrid[0:h + 2 * pad, 0:w + 2 * pad].astype(np.float64)
    t = np.clip(gradient_t(g, float(w), float(h), (xs - pad + 0.5, ys - pad + 0.5)), 0.0, 1.0)
    stops = sorted((s.offset, s.alpha) for s in fade.stops)
    return np.interp(t, [o for o, _ in stops], [v for _, v in stops]).astype(np.float32)


def _similarity(F: np.ndarray, fill_lab: np.ndarray) -> np.ndarray:
    """1 where the texture colour is the fill's, 0 beyond ΔE 20 (a stroke ring, a shadow inside the box)."""
    de = delta_e(srgb_to_lab(F.astype(np.float32)), fill_lab)
    lo, hi = SIM_DE
    return np.clip((hi - de) / (hi - lo), 0.0, 1.0).astype(np.float32)


def _ramp_to_plate(F, alpha, B, interior, grad: Gradient):
    """A fill ramp whose colours lie on the line from the plate to one end colour f is that colour fading out: a
    static plate cannot tell the two apart, and a fade keeps the plate showing through after a background edit.
    Returns (f, α factor per pixel, p90 ΔE of the two-colour fit) or None."""
    m = interior & (alpha > SOLID_A)
    if m.sum() < 30:
        return None
    ends = [np.float32(hex_to_rgb8(s.color)) for s in (grad.stops[0], grad.stops[-1])]
    Bm = B[m].mean(0)
    f = max(ends, key=lambda c: float(np.linalg.norm(c - Bm)))
    d = f - B
    dd = (d * d).sum(-1)
    ok = dd >= 20.0 ** 2
    a2 = np.where(ok, ((F - B) * d).sum(-1) / np.maximum(dd, 1e-6), 1.0).clip(0.0, 1.0).astype(np.float32)
    mm = m & ok
    if mm.sum() < 0.8 * m.sum():
        return None
    pred = a2[..., None] * f + (1 - a2[..., None]) * B
    res = delta_e(srgb_to_lab(F[mm]), srgb_to_lab(pred[mm].astype(np.float32)))
    v = a2[mm]
    p90 = float(np.percentile(res, 90))
    if p90 > 4.0 or float(np.percentile(v, 95) - np.percentile(v, 5)) <= FADE_RANGE:
        return None
    return f, np.where(alpha > INK_A, a2, 1.0).astype(np.float32), p90


def _fill_coverage(a: np.ndarray, F: np.ndarray, fill, sim: np.ndarray) -> np.ndarray:
    """The fill's own coverage: α where the texture shows the fill. Where the fill meets another solid colour (a
    stroke ring, a shadow inside the box; matting keeps such a third colour as a hard edge with the observed mix),
    the fill's share by projection on fill − that colour (taken a pixel away from the fill)."""
    cov = a * sim
    core = (sim >= 0.999) & (a > SOLID_A)
    other = (a > SOLID_A) & (sim <= 0.0)
    if not other.any() or not core.any():
        return cov
    near_core = cv2.dilate(core.astype(np.uint8), _K3) > 0
    pure = other & ~near_core
    o = F.reshape(-1, 3)[matting._nearest(pure if pure.any() else other).ravel()].reshape(F.shape)
    d = np.broadcast_to(np.asarray(fill, np.float32), F.shape) - o
    dd = (d * d).sum(-1)
    beta = np.clip(((F - o) * d).sum(-1) / np.maximum(dd, 1e-6), 0.0, 1.0)
    m = (a > INK_A) & ~core & (dd >= 20.0 ** 2) & (cv2.dilate(core.astype(np.uint8), _disc(2)) > 0)
    cov[m] = (a * beta)[m]
    return cov


# --- effects ------------------------------------------------------------------------------------------------------

def _predict(alpha, fill, effects, B, fade=None):
    """The frame the renderer would draw: compose_text over B (0..255), and the total α."""
    rgba = compose_text(alpha, fill, effects)
    if fade is not None:
        rgba[..., 3] *= fade
    A = rgba[..., 3:4]
    return rgba[..., :3] * 255.0 * A + (1.0 - A) * B, rgba[..., 3]


class _Region:
    """The pixels effects are measured on (outside the glyphs), with a plane per channel taken out of every residual
    there: a plate that is off by a smooth field (an estimate, an animated background kept still) is no effect."""

    def __init__(self, omega: np.ndarray):
        self.mask = omega
        ys, xs = np.nonzero(omega)
        h, w = omega.shape
        X = np.stack([np.ones(len(xs)), (xs - w / 2) / max(w, 1), (ys - h / 2) / max(h, 1)], 1)
        self.Q = np.linalg.qr(X)[0].astype(np.float32)

    def perp(self, r: np.ndarray) -> np.ndarray:
        """r (N×k over the region) less its plane."""
        return r - self.Q @ (self.Q.T @ r)

    def energy(self, I: np.ndarray, P: np.ndarray) -> float:
        r = self.perp((I - P)[self.mask])
        return float((r * r).sum())


class _Xcorr:
    """c[dy + lim, dx + lim] = Σ_x g(x)·k(x − (dx, dy)), zero outside both arrays, for |dx|, |dy| ≤ lim; the
    spectra of the g side are kept across calls."""

    def __init__(self, shape: tuple[int, int], lim: int):
        h, w = shape
        self.size = (cv2.getOptimalDFTSize(h + lim + 1), cv2.getOptimalDFTSize(w + lim + 1))
        r = np.arange(-lim, lim + 1)
        self.ix = np.ix_(r % self.size[0], r % self.size[1])

    def spectrum(self, a: np.ndarray) -> np.ndarray:
        return np.fft.rfft2(a, self.size)

    def __call__(self, G: np.ndarray, k: np.ndarray) -> np.ndarray:
        return np.fft.irfft2(G * np.conj(self.spectrum(k)), self.size)[self.ix]


def _colour_fit(y: np.ndarray, s: np.ndarray, b: np.ndarray, region: _Region) -> tuple[np.ndarray, float] | None:
    """Least squares over the region for y = op·s·(c − b) + plane (y, b: N×3, s: N): opacity on a grid, colour in
    closed form per opacity. Ties (a flat plate cannot tell them apart) go to the lowest opacity, i.e. the most
    saturated colour."""
    sp = region.perp(s[:, None].astype(np.float32))[:, 0]
    A = float((sp * sp).sum())
    if A < 1e-6:
        return None
    yp, sbp = region.perp(y), region.perp(s[:, None] * b)
    Bv, Cv = (sp[:, None] * yp).sum(0), (sp[:, None] * sbp).sum(0)
    Dv, Ev, Fv = (yp * yp).sum(0), (yp * sbp).sum(0), (sbp * sbp).sum(0)
    rows = []
    for op in OPACITIES:   # z = y + op·s·b = op·s·c + plane: per channel c = ⟨k⊥, z⊥⟩ / ⟨k⊥, k⊥⟩, k = op·s
        kk, kz = op * op * A, op * Bv + op * op * Cv
        c = np.clip(kz / kk, 0.0, 255.0)
        zz = Dv + 2 * op * Ev + op * op * Fv
        rows.append((float((zz - 2 * c * kz + c * c * kk).sum()), float(op), c))
    e_min = min(e for e, _, _ in rows)
    _, op, c = next(row for row in rows if row[0] <= e_min + 1e-3 * abs(e_min) + 1e-6)
    return c, op


def _lum(rgb) -> np.ndarray:
    return srgb_to_lab(np.asarray(rgb, np.float32))[..., 0]


def _fit_stroke(I, B, alpha, fill, cap, region, effects, fade, fill_lab):
    best = None
    for w in STROKE_WIDTHS:
        if w > STROKE_MAX * cap:
            break
        ring = region.mask & (_dilate(alpha, float(w)) >= 0.98)
        if ring.sum() < 8:
            continue
        lab = srgb_to_lab(I[ring])
        med = np.median(lab, axis=0).astype(np.float32)
        if float(np.percentile(delta_e(lab, med), 75)) >= RING_SPREAD or float(delta_e(med, fill_lab)) < EFFECT_FILL_DE:
            continue
        e = TextEffect(kind="stroke", color=rgb8_to_hex(lab_to_srgb(med)), width=float(w))
        E = region.energy(I, _predict(alpha, fill, [*effects, e], B, fade)[0])
        if best is None or E < best[1]:
            best = (e, E)
    return best


def _fit_blurred(kind, I, B, alpha, fill, cap, region, effects, fade, fill_lab):
    """Shadow (darkening; offsets within ±0.3 cap, σ ∈ {0,1,2,4,8}) or glow (lightening; no offset, σ ∈ {2,4,8,16}):
    the shape by the best per-channel least-squares fit of the residual to the shifted, blurred glyphs where they
    would show (under the fill and strokes nothing does), then colour and opacity by least squares. An effect in
    the fill's own colour is refused."""
    P, A = _predict(alpha, fill, effects, B, fade)
    omega = region.mask
    vis = np.where(omega, 1.0 - A, 0.0).astype(np.float32)
    R = np.zeros_like(I)
    R[omega] = region.perp((I - P)[omega])
    Y = R * vis[..., None]
    lim = max(1, int(SHADOW_REACH * cap)) if kind == "shadow" else 0
    if lim:
        xc = _Xcorr(alpha.shape, lim)
        GY = [xc.spectrum(Y[..., c]) for c in range(3)]
        GV = xc.spectrum(vis * vis)
    best = None
    for sigma in (SHADOW_SIGMAS if kind == "shadow" else GLOW_SIGMAS):
        S = _blur(alpha, float(sigma)) if sigma else alpha
        if lim:
            num = np.stack([xc(G, S) for G in GY], -1)
            den = xc(GV, S * S)
        else:
            num = np.array([[[(Y[..., c] * S).sum() for c in range(3)]]])
            den = np.array([[(vis * vis * S * S).sum()]])
        sign = num.sum(-1)
        ok = (den > 1e-6) & ((sign < 0) if kind == "shadow" else (sign > 0))
        score = np.where(ok, (num ** 2).sum(-1) / np.maximum(den, 1e-6), 0.0)
        j = np.unravel_index(int(np.argmax(score)), score.shape)
        if score[j] > 0 and (best is None or score[j] > best[0]):
            best = (float(score[j]), sigma, j[1] - lim, j[0] - lim)
    if best is None:
        return None
    _, sigma, dx, dy = best
    S = _shift(_blur(alpha, float(sigma)) if sigma else alpha, float(dx), float(dy)) * vis
    fit = _colour_fit((I - P)[omega], S[omega], B[omega], region)
    if fit is None:
        return None
    c, op = fit
    m = S > 1e-3
    under = float(np.average(_lum(B[m]), weights=S[m]))
    if (kind == "shadow" and _lum(c) >= under) or (kind == "glow" and _lum(c) <= under):
        return None
    if float(delta_e(srgb_to_lab(np.float32(c)), fill_lab)) < EFFECT_FILL_DE:
        return None
    e = TextEffect(kind=kind, color=rgb8_to_hex(np.rint(c)), opacity=op, dx=float(dx), dy=float(dy), blur=float(2 * sigma))
    return e, region.energy(I, _predict(alpha, fill, [*effects, e], B, fade)[0])


def fit_effects(I, B, alpha, fill_rgb, cap: float, *, valid=None, fade=None) -> list[TextEffect]:
    """Stroke / shadow / glow (at most one of each) that explain the residual R = I − (αF + (1 − α)B) outside the
    glyphs (α > 0.05), each kept only when it cuts the residual energy there by ≥ 30 %. A plane per channel is taken
    out of the residual first (a smooth plate error is no effect). I, B: the wide crop and the plate behind it
    (0..255); alpha: the fill's glyph coverage; fill_rgb: colour or H×W×3 (0..255); fade: α mask."""
    I = np.asarray(I, np.float32)
    B = np.asarray(B, np.float32)
    alpha = _a01(alpha)
    fill = np.asarray(fill_rgb, np.float32) / 255.0
    omega = alpha <= INK_A
    if valid is not None:
        omega &= np.asarray(valid, bool)
    if cap < 4 or omega.sum() < 16:
        return []
    region = _Region(omega)
    fill_lab = srgb_to_lab(np.float32(np.median(np.broadcast_to(fill, I.shape)[alpha > SOLID_A], axis=0) * 255)
                           if (alpha > SOLID_A).any() else np.float32([0, 0, 0]))
    effects: list[TextEffect] = []
    E = region.energy(I, _predict(alpha, fill, effects, B, fade)[0])
    left = ["stroke", "shadow", "glow"]
    while left and E > 1e-6:
        cands = []
        for kind in left:
            got = (_fit_stroke(I, B, alpha, fill, cap, region, effects, fade, fill_lab) if kind == "stroke"
                   else _fit_blurred(kind, I, B, alpha, fill, cap, region, effects, fade, fill_lab))
            if got is not None and got[1] <= (1 - EFFECT_DROP) * E:
                cands.append(got)
        if not cands:
            break
        e, E = min(cands, key=lambda c: c[1])
        effects.append(e)
        left.remove(e.kind)
    return effects


# --- the element --------------------------------------------------------------------------------------------------

def _effect_crop(raw, alpha_tex, frames, plate, others_at, pad: int, meta: TextureMeta):
    """I and B averaged over up to 4 of the matted frames, warped into the texture space padded by `pad`."""
    pool = [f for f in meta.frames if 0 <= f < min(len(raw), len(frames)) and np.isfinite(raw[f, :2]).all()]
    if not pool:
        return None
    pick = sorted({pool[i] for i in np.rint(np.linspace(0, len(pool) - 1, min(EFFECT_FRAMES, len(pool)))).astype(int)})
    samples = matting.gather_samples(raw, alpha_tex > SOLID_A, frames, plate, others_at, pad, candidates=pick)
    if not samples:
        return None
    n = np.zeros(samples[0].valid.shape, np.float32)
    Isum = np.zeros(samples[0].I.shape, np.float32)
    Bsum = np.zeros_like(Isum)
    for s in samples:
        v = s.valid.astype(np.float32)[..., None]
        n += v[..., 0]
        Isum += s.I * v
        Bsum += s.B * v
    k = np.maximum(n, 1.0)[..., None]
    return Isum / k, Bsum / k, n > 0


def _scaled(arrs, f: float):
    out = []
    for a in arrs:
        h, w = a.shape[:2]
        size = (max(1, int(round(w * f))), max(1, int(round(h * f))))
        out.append(cv2.resize(a.astype(np.float32), size, interpolation=cv2.INTER_AREA))
    return out


def analyse_text_style(rgba, meta: TextureMeta | None, frames, plate, raw, text: str, *,
                       others_at: Callable | None = None, core: str | None = None) -> tuple[TextStyle, str, dict]:
    """(TextStyle, fill hex, info) for a text element from its matted texture (un-premultiplied RGBA, padding 0) and
    the frames / plate it was matted from. core: today's core-mask colour (hex), the tie-breaker when the fill
    measured may be the surroundings the matte kept (`core_fill`). info: stroke_px, cap_px, spread, unfilled (the
    share of stroke centres the fill does not draw), centres, alpha (the fill's glyph coverage, unfaded) and
    seconds."""
    t0 = time.perf_counter()
    rgba = np.asarray(rgba)
    meta = meta or TextureMeta(method="binary", frames=[], confidence=matting.METHOD_FACTOR["binary"])
    a_tex = rgba[..., 3].astype(np.float32) / 255.0
    F = rgba[..., :3].astype(np.float32)
    h, w = a_tex.shape
    if not (a_tex > SOLID_A).any():
        raise ValueError("texture has no solid glyph pixels")
    lines = max(1, len([ln for ln in (text or "").split("\n") if ln.strip()]))
    matted = meta.method != "binary"
    cap0 = cap_height(a_tex, lines)
    pe = int(max(4, round(0.5 * cap0)))
    crop = _effect_crop(raw, a_tex, frames, plate, others_at, pe, meta) if cap0 >= 4 else None
    B_box = crop[1][pe:pe + h, pe:pe + w] if crop is not None else None
    edge = np.concatenate([F[0], F[-1], F[:, 0], F[:, -1]])
    if B_box is not None and (a_tex >= 0.98).mean() >= BOX_SHARE and float(delta_e(
            _mode(srgb_to_lab(edge)), srgb_to_lab(np.median(B_box.reshape(-1, 3), axis=0)))) < KEPT_PLATE_DE:
        # the matte kept its box and the plate in it: colour and glyph measures only, glyphs by contrast
        a_tex = contrast_glyphs(F, B_box).astype(np.float32)
        matted = False
    elif B_box is not None:   # texture pixels that are the local plate (a texture that kept it) are not glyph
        fg = delta_e(srgb_to_lab(F), srgb_to_lab(B_box)) >= FG_DE
        if (fg & (a_tex > SOLID_A)).sum() >= 20:
            a_tex = a_tex * fg
    sw0 = stroke_width(a_tex)
    hexv, lab, spread = fill_colour(np.dstack([F, a_tex * 255.0]), sw0, B_box)
    known = B_box[crop[2][pe:pe + h, pe:pe + w]] if B_box is not None else None   # plate pixels with data
    glyph = core_fill(F, a_tex, lab, srgb_to_lab(np.float32(hex_to_rgb8(core))), known) if core else None
    if glyph is not None:   # the fill was the surroundings the matte kept: the glyphs are the solid pixels not them
        a_tex = ((a_tex > SOLID_A) & (delta_e(srgb_to_lab(F), lab) >= FG_DE)).astype(np.float32)
        matted = False
        sw0 = stroke_width(a_tex)
        m = interior_mask(a_tex, sw0)
        lab, hexv = glyph, rgb8_to_hex(lab_to_srgb(glyph))
        spread = float(np.percentile(delta_e(srgb_to_lab(F[m]), lab), 90)) if m.any() else spread
    sim = _similarity(F, lab)
    # glyph interiors at any fade level: α near its local maximum
    plateau = (a_tex > INK_A) & (a_tex >= 0.85 * cv2.dilate(a_tex, _disc(2)))
    plateau = cv2.erode(plateau.astype(np.uint8), _K3) > 0 if plateau.sum() > 200 else plateau
    a_g = a_tex * sim
    grad = fit_fill_gradient(F, interior_mask(a_g, sw0), interior_mask(a_tex, sw0))
    fade = None
    if grad is not None and B_box is not None:   # a colour ramp toward the plate: one colour fading out?
        two = _ramp_to_plate(F, a_tex, B_box, interior_mask(a_tex, sw0), grad)
        if two is not None:
            f_rgb, a2, residual = two
            fade = fit_fade(a_tex * a2, plateau)
            if fade is not None:
                a_tex = a_tex * a2
                F = np.broadcast_to(f_rgb, F.shape).astype(np.float32)
                lab = srgb_to_lab(f_rgb)
                hexv, grad, sim, spread = rgb8_to_hex(np.rint(f_rgb)), None, np.ones_like(a_tex), residual
    if fade is None and matted:
        fade = fit_fade(a_tex, plateau & (sim > 0.5))
    fmap = fade_map(fade, w, h) if fade is not None else None
    a_n = np.clip(a_tex / np.maximum(fmap, 0.05), 0.0, 1.0) if fmap is not None else a_tex
    fill_map = render_gradient(grad, w, h).astype(np.float32) if grad is not None else None
    if fill_map is not None:
        sim = np.clip((SIM_DE[1] - delta_e(srgb_to_lab(F), srgb_to_lab(fill_map))) / (SIM_DE[1] - SIM_DE[0]), 0.0, 1.0)
    centres = stroke_centres(a_tex, sw0)   # those the fill (flat or ramp) does not draw: a second colour
    unfilled = float((sim[centres] <= 0).mean()) if centres.sum() >= 20 else 0.0
    a_g = _fill_coverage(a_n, F, fill_map if fill_map is not None else lab_to_srgb(lab).astype(np.float32), sim)
    cap = cap_height(a_g, lines)
    effects: list[TextEffect] = []
    if crop is not None and matted and cap >= 4:
        I, B, valid = crop
        alpha_p = np.pad(a_g, pe)
        fill_p = (np.pad(fill_map, ((pe, pe), (pe, pe), (0, 0)), mode="edge") if fill_map is not None
                  else np.float32(lab_to_srgb(lab)))
        fade_p = fade_map(fade, w, h, pe) if fade is not None else None
        f = min(1.0, CAP_WORK / cap)
        if f < 1.0:   # large titles: search at a working scale, report in texture pixels
            arrs = [I, B, valid.astype(np.float32), alpha_p] + ([fill_p] if fill_p.ndim == 3 else []) + ([fade_p] if fade_p is not None else [])
            sc = _scaled(arrs, f)
            I_s, B_s, v_s, a_s = sc[:4]
            rest = sc[4:]
            fill_s = rest.pop(0) if fill_p.ndim == 3 else fill_p
            fade_s = rest.pop(0) if fade_p is not None else None
            found = fit_effects(I_s, B_s, a_s, fill_s, cap * f, valid=v_s > 0.99, fade=fade_s)
            effects = [e.model_copy(update={"width": round(e.width / f, 2), "dx": round(e.dx / f, 2), "dy": round(e.dy / f, 2),
                                            "blur": round(e.blur / f, 2)}) for e in found]
        else:
            effects = fit_effects(I, B, alpha_p, fill_p, cap, valid=valid, fade=fade_p)
    sw = stroke_width(a_g)
    conf = float(np.clip(meta.confidence, 0.0, 1.0)) if matted else min(0.5, float(meta.confidence))
    if (spread > GRADIENT_DE and grad is None) or unfilled > UNFILLED:
        conf *= 0.8
    style = TextStyle(fill=grad, fade=fade, effects=effects, stroke_ratio=round(sw / cap, 4) if cap > 0 and sw > 0 else None,
                      confidence=round(conf, 3))
    info = {"stroke_px": round(sw, 3), "cap_px": round(cap, 3), "spread": round(spread, 3),
            "unfilled": round(unfilled, 3), "centres": glyph_centres(a_g, text or ""), "alpha": a_g,
            "seconds": round(time.perf_counter() - t0, 4)}
    return style, hexv, info


# --- the pipeline phase -------------------------------------------------------------------------------------------

def _font_job(k: str, p: dict, style: TextStyle | None, info: dict | None):
    """The font-matching input of a text element: the fill coverage the style analysis left (the texture's alpha
    when it failed), with its stroke ratio and glyph centres."""
    from ..fonts.match import TextJob
    text = p.get("text") or ""
    if info is not None:
        alpha, centres, ratio = info["alpha"], info.get("centres"), style.stroke_ratio if style is not None else None
    else:
        alpha = np.asarray(p["canon"])[..., 3].astype(np.float32) / 255.0
        centres, ratio = glyph_centres(alpha, text), None
    return TextJob(k, alpha, text, ratio, centres, p.get("font"))


def style_props(props: dict, frames: np.ndarray, plate, *, workers: int = 4, fonts=None) -> dict:
    """The text style of every text element in `props` (in place), after textures v2: `style` (TextStyle dump),
    `color` (the analysed fill), `core_color` (today's core-mask colour, the fallback) and `style_info`. An element
    whose analysis fails keeps the core-mask colour, no style, and `style_error` = STYLE_FAILED.
    Then the fonts (Task 10, `fonts`: the project's FontRegistry), one text after another outside the thread pool
    (`fonts.match.font_guesses`: deterministic, capped by work): `font` (FontGuess) and the style's tracking, shear
    and offset. A text that fails keeps the first guess at confidence ≤ 0.5 with `font_error` = MATCH_FAILED; one
    past the scene's work cap the same with `font_skipped` = WORK_CAP. Stage data and report messages carry these
    codes only (they ship in project ZIPs and reach the browser); exception details go to the server log, paths
    cut."""
    from ..fonts import match as fontmatch
    from ..fonts.registry import FontRegistry
    registry = fonts or FontRegistry()
    t0 = time.perf_counter()
    elements = [k for k, p in props.items() if not k.startswith("_") and isinstance(p, dict) and "canon" in p and "raw" in p]
    keys = [k for k in elements if props[k].get("kind") == "text"]
    if not keys:
        return {}
    plate = matting.as_plate(plate)
    others_for = matting._layers(props, elements, frames, plate)

    def run(k):
        p = props[k]
        meta = TextureMeta(**p["texture_meta"]) if p.get("texture_meta") else None
        try:
            return k, *analyse_text_style(p["canon"], meta, frames, plate, p["raw"], p.get("text") or "",
                                          others_at=others_for(k), core=p.get("core_color") or p.get("color")), None
        except Exception as e:   # fail soft: today's core-mask colour, no style, a code (the detail is logged)
            log.warning("text style failed for %s: %s", k, describe(e, trace=True))
            return k, None, None, None, STYLE_FAILED

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        results = list(ex.map(run, keys))
    t1 = time.perf_counter()
    try:
        found = fontmatch.font_guesses([_font_job(k, props[k], style, info) for k, style, _, info, _ in results],
                                       registry)
    except Exception as e:   # fail soft: every text keeps its first guess
        log.warning("font matching failed: %s", describe(e, trace=True))
        found = {k: ("error", fontmatch.MATCH_FAILED) for k in keys}
    t2 = time.perf_counter()
    failed, per, counts = 0, [], {"ok": 0, "error": 0, "skipped": 0}
    for k, style, colour, info, err in results:
        p = props[k]
        p.setdefault("core_color", p.get("color"))
        for name in ("style", "style_error", "font_error", "font_skipped"):
            p.pop(name, None)
        res = found.get(k, ("error", fontmatch.MATCH_FAILED))
        counts[res[0]] += 1
        if res[0] == "ok":
            p["font"] = res[1]
            if style is not None:
                style = style.model_copy(update=res[2])
        else:
            prior = p.get("font")
            if prior is not None:
                p["font"] = prior.model_copy(update={"confidence": min(0.5, prior.confidence)})
            p["font_error" if res[0] == "error" else "font_skipped"] = res[1]
        if err is not None:
            p["color"] = p["core_color"]
            p["style_error"] = err
            failed += 1
            continue
        p["style"], p["color"] = style.model_dump(), colour
        p["style_info"] = {name: info[name] for name in ("stroke_px", "cap_px", "spread", "centres", "seconds")}
        per.append(info["seconds"])
    log.info("sprites text style elements=%s failed=%s per-element max %.3fs %.2fs", len(keys), failed,
             max(per, default=0.0), t1 - t0)
    log.info("sprites text font elements=%s matched=%s failed=%s skipped=%s %.2fs", len(keys), counts["ok"],
             counts["error"], counts["skipped"], t2 - t1)
    return {"elements": len(keys), "failed": failed, "font_failed": counts["error"], "font_skipped": counts["skipped"]}
