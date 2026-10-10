"""Fit an editable linear or radial gradient to a plate, scored against exactly what `render_gradient` draws."""
from __future__ import annotations
import math
import cv2, numpy as np
from ..ir.colour import delta_e, hex_to_rgb8, lab_to_srgb, rgb8_to_hex, srgb_to_lab
from ..ir.gradient import gradient_t, render_gradient
from ..ir.schema import Gradient, GradientStop

BINS = 64
DP_TOL = 1.0              # ΔE76: a bin further than this from the stops' interpolation becomes a stop
WORK = 160
MEASURE = 640             # long side (px) of the full-size check
ANGLES = np.arange(0.0, 360.0, 5.0)
REFINE = np.arange(-2.5, 2.51, 0.5)
WARM = np.arange(-10.0, 10.1, 2.5)    # angles around the previous key's
GRID = (0.0, 0.25, 0.5, 0.75, 1.0)
STEP_MIN = 0.004          # radial centre search stops below this step (fraction of the frame)
MOVES = [(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy]   # diagonals follow diagonal valleys
PREFER_LINEAR = 0.1       # ΔE: radial must beat linear by this much
COARSE_PX = 4000          # pixels the coarse angle / centre grids are scored on
PRUNE_DE = 0.25           # ΔE: an inner stop whose removal raises p95 by at most this goes
Q = 1024                  # Lab quantisation levels for the per-bin medians
_QS = np.array([10.0, 4.0, 4.0])
_QO = np.array([0.0, 128.0, 128.0])
_T = np.linspace(0.0, 1.0, 1025)
_DUMMY = [GradientStop(offset=0, color="#000000"), GradientStop(offset=1, color="#ffffff")]


class _Data:
    """Valid pixels of a work-size plate (float: area averages keep sub-level precision, so even a gradient
    spanning a few 8-bit levels has a well-defined angle): frame coordinates, sRGB, Lab, quantised Lab."""

    def __init__(self, plate_small, valid, size):
        h, w = plate_small.shape[:2]
        self.W, self.H = size or (w, h)
        ys, xs = np.mgrid[0:h, 0:w]
        m = np.ones((h, w), bool) if valid is None else np.asarray(valid, bool)
        self.x = (xs[m] + 0.5) * self.W / w
        self.y = (ys[m] + 0.5) * self.H / h
        self.rgb = plate_small[m].astype(np.float64)
        self.lab = srgb_to_lab(self.rgb)
        self.q = np.clip(np.rint((self.lab + _QO) * _QS), 0, Q - 1).astype(np.int64)

    def every(self, k: int) -> "_Data":
        """Every k-th pixel: the coarse searches run on this."""
        out = object.__new__(_Data)
        out.W, out.H = self.W, self.H
        for name in ("x", "y", "rgb", "lab", "q"):
            setattr(out, name, getattr(self, name)[::k])
        return out


def _medians(b, q, t):
    """Per-bin medians of t (bins are runs of sorted t) and of each quantised Lab channel; returns the
    non-empty bins, their median t and median Lab."""
    cnt = np.bincount(b, minlength=BINS)
    nz = np.flatnonzero(cnt)
    start = (np.cumsum(cnt) - cnt)[nz]
    lo, hi = start + (cnt[nz] - 1) // 2, start + cnt[nz] // 2
    ts = np.sort(t)
    tm = (ts[lo] + ts[hi]) / 2
    med = np.empty((len(nz), 3))
    for c in range(3):
        s = np.sort(b * Q + q[:, c]) - np.repeat(np.arange(BINS) * Q, cnt)
        med[:, c] = (s[lo] + s[hi]) / 2
    return nz, tm, med / _QS - _QO


def _knots(t, data, max_stops, n_stops=None, tail=False):
    """Stop offsets: bin t, take each bin's median Lab (at its median t), then add the worst bin as a stop
    (Douglas–Peucker on ΔE, in sRGB interpolation as CSS draws it) until every bin is within DP_TOL or the
    budget is spent. `tail`: a flat run after the last real stop costs nothing (the render clamps), as when
    a radial gradient ends inside the frame."""
    b = np.minimum((t * BINS).astype(np.int64), BINS - 1)
    nz, tc, med = _medians(b, data.q, t)
    if len(nz) < 2:
        return None
    rgb = lab_to_srgb(med).astype(np.float64)
    knots, order = [0, len(nz) - 1], []
    limit = (n_stops or max_stops) + (1 if tail else 0)
    while len(knots) < limit:
        pred = np.stack([np.interp(tc, tc[knots], rgb[knots, c]) for c in range(3)], -1)
        err = delta_e(srgb_to_lab(pred), med)
        err[knots] = -1
        k = int(np.argmax(err))
        if err[k] <= (DP_TOL if n_stops is None else -1):
            break
        knots = sorted(knots + [k])
        order.append(k)
    if tail and len(knots) > (n_stops or max_stops):
        flat = delta_e(srgb_to_lab(rgb[knots[-2]:]), srgb_to_lab(rgb[knots[-2]])).max() <= DP_TOL
        knots.remove(knots[-1] if flat else order[-1])
    offs = tc[knots]
    offs[0] = max(0.0, nz[knots[0]] / BINS)
    if not (tail and knots[-1] < len(nz) - 1):
        offs[-1] = min(1.0, (nz[knots[-1]] + 1) / BINS)
    return np.round(offs, 4)


def _solve(t, data, offs):
    """Least-squares sRGB stop colours for fixed offsets (render_gradient is linear in them), rounded to 8 bit;
    returns the colours and the p95 ΔE on the valid pixels of the render (averaged to work size, i.e. not
    rounded per pixel)."""
    t = np.clip(t, 0.0, 1.0)
    eye = np.eye(len(offs))
    a = np.stack([np.interp(t, offs, eye[k]) for k in range(len(offs))], -1)
    cols = np.clip(np.rint(np.linalg.lstsq(a.T @ a, a.T @ data.rgb, rcond=None)[0]), 0, 255)
    curve = srgb_to_lab(np.stack([np.interp(_T, offs, cols[:, c]) for c in range(3)], -1))   # the colour is a function of t
    pred = np.stack([np.interp(t, _T, curve[:, c]) for c in range(3)], -1)
    return cols.astype(int), float(np.percentile(delta_e(pred, data.lab), 95))


def _try(data, kind, max_stops, n_stops, prune=False, offsets=None, **geom):
    """The gradient of this geometry and its p95, with stops placed by Douglas–Peucker or at `offsets`.
    `prune`: drop inner stops while the p95 rises by ≤ PRUNE_DE (bin noise can make Douglas–Peucker add a
    stop the least-squares colours do not need)."""
    probe = Gradient(kind=kind, stops=_DUMMY, **geom)
    t = gradient_t(probe, data.W, data.H, (data.x, data.y))
    offs = _knots(np.clip(t, 0.0, 1.0), data, max_stops, n_stops, tail=kind == "radial") if offsets is None else offsets
    if offs is None:
        return None, math.inf
    cols, p95 = _solve(t, data, offs)
    while prune and n_stops is None and offsets is None and len(offs) > 2:
        c, q, o = min(((*_solve(t, data, o), o) for o in (np.delete(offs, k) for k in range(1, len(offs) - 1))),
                      key=lambda r: r[1])
        if q > p95 + PRUNE_DE:
            break
        offs, cols, p95 = o, c, q
    stops = [GradientStop(offset=float(o), color=rgb8_to_hex(c)) for o, c in zip(offs, cols)]
    return Gradient(kind=kind, stops=stops, **geom), p95


def _radius(cx, cy, W, H) -> float:
    far = max(math.hypot(cx * W - x, cy * H - y) for x in (0, W) for y in (0, H))
    return max(far, 1e-6) / (math.hypot(W, H) / 2)


def fit_linear(plate_small, valid, max_stops=4, *, size=None, angles=None, n_stops=None, offsets=None, data=None):
    """Best linear gradient over `angles` (default 0..355° step 5), refined ±2.5° step 0.5. `size` = (W, H)
    of the full frame the angle refers to; `n_stops` forces the stop count, `offsets` the stop offsets.
    Returns (Gradient | None, p95)."""
    data = data or _Data(plate_small, valid, size)
    if not len(data.x):
        return None, math.inf
    coarse = data.every(max(1, len(data.x) // COARSE_PX))
    centre = min((a % 360 for a in (ANGLES if angles is None else angles)),
                 key=lambda a: _try(coarse, "linear", max_stops, n_stops, offsets=offsets, angle=float(a))[1])
    best = min(((centre + d) % 360 for d in REFINE),
               key=lambda a: _try(data, "linear", max_stops, n_stops, offsets=offsets, angle=float(a))[1])
    return _try(data, "linear", max_stops, n_stops, prune=True, offsets=offsets, angle=float(best))


def fit_radial(plate_small, valid, max_stops=3, *, size=None, center=None, n_stops=None, offsets=None, data=None):
    """Best radial gradient: centre from a 5×5 grid over the frame (or from `center`), then a shrinking
    8-way pattern search; the radius reaches the farthest corner. Returns (Gradient | None, p95)."""
    data = data or _Data(plate_small, valid, size)
    if not len(data.x):
        return None, math.inf

    def score(c, d=data):
        c = (min(1.5, max(-0.5, c[0])), min(1.5, max(-0.5, c[1])))
        g, p = _try(d, "radial", max_stops, n_stops, offsets=offsets, center=c, radius=_radius(*c, d.W, d.H))
        return p, g, c

    if center is None:
        coarse = data.every(max(1, len(data.x) // COARSE_PX))
        center, step = min((score((x, y), coarse) for x in GRID for y in GRID), key=lambda r: r[0])[2], 0.125
    else:
        step = 0.03
    p, g, c = score(center)
    while step >= STEP_MIN:
        q, h, d = min((score((c[0] + dx * step, c[1] + dy * step)) for dx, dy in MOVES), key=lambda r: r[0])
        if q < p:
            p, g, c = q, h, d
        else:
            step /= 2
    return _try(data, "radial", max_stops, n_stops, prune=True, offsets=offsets, center=c, radius=_radius(*c, data.W, data.H))


def shrink(img, valid, work: int = WORK):
    """Area-resized float image (max side ≤ work) and the pixels whose whole footprint is valid."""
    H, W = img.shape[:2]
    s = min(1.0, work / max(W, H))
    w, h = max(1, round(W * s)), max(1, round(H * s))
    if (w, h) == (W, H):
        return img.astype(np.float32), (None if valid is None else np.asarray(valid, bool))
    small = cv2.resize(img.astype(np.float32), (w, h), interpolation=cv2.INTER_AREA)
    return small, (None if valid is None else _footprint(valid, (w, h)))


def _footprint(valid, size) -> np.ndarray:
    """Pixels of the `size` (w, h) area reduction of a mask whose whole footprint is valid."""
    return cv2.resize(np.asarray(valid, np.uint8) * 255, size, interpolation=cv2.INTER_AREA) == 255


def measure(g: Gradient, plate, valid=None) -> float:
    """p95 ΔE76 between the plate and the render of g, both area-resized to ≤ MEASURE px long side, over
    pixels whose footprint is valid: the full-size check that a fit made on ≤ 160 px averages has not erased
    fine structure. No blur: a 3×3 blur would erase a 2-px pattern (±12 → 0.57 ΔE) along with the codec
    noise, which stays well inside the bound anyway (yuv420p crf 23: 1.3–1.6)."""
    H, W = plate.shape[:2]
    s = min(1.0, MEASURE / max(W, H))
    size = (max(1, round(W * s)), max(1, round(H * s)))

    def prep(img):
        img = img.astype(np.float32)
        return cv2.resize(img, size, interpolation=cv2.INTER_AREA) if size != (W, H) else img

    m = np.ones(size[::-1], bool) if valid is None else _footprint(valid, size)
    if not m.any():
        return math.inf
    a, b = prep(plate)[m], prep(render_gradient(g, W, H))[m]
    return float(np.percentile(delta_e(srgb_to_lab(a), srgb_to_lab(b)), 95))


def fit_gradient(plate, valid, *, work=WORK, size=None):
    """The better of a ≤ 4-stop linear and a ≤ 3-stop radial fit at ≤ `work` px (linear unless radial is
    clearly better) and its p95 ΔE76 there: render and plate both area-averaged to the work size, so 8-bit
    banding and codec noise do not count (per pixel, a dark gradient a few levels deep misses by ~1.6 ΔE
    unless the fit is bit-exact); `measure` is the full-size check. `size`: the frame (W, H) when `plate`
    is already reduced. Returns (Gradient | None, p95)."""
    H, W = plate.shape[:2]
    small, v = shrink(plate, valid, work)
    data = _Data(small, v, size or (W, H))
    if len(data.x) < 16:
        return None, math.inf
    lin, p_lin = fit_linear(None, None, data=data)
    rad, p_rad = fit_radial(None, None, data=data)
    return (rad, p_rad) if p_rad < p_lin - PREFER_LINEAR else (lin, p_lin)


def stop_range(g: Gradient) -> float:
    """Largest ΔE76 between two stops."""
    lab = srgb_to_lab(np.array([hex_to_rgb8(s.color) for s in g.stops], np.float64))
    return float(delta_e(lab[:, None], lab[None]).max())


def fit_like(small, valid, size, like: Gradient | None = None, *, warm: bool = True):
    """Fit a work-size image (frame geometry `size`): a fresh search, or one with `like`'s kind and stop count
    (keys of an animated gradient must match to interpolate): warm-started from its geometry with its stop
    offsets, or (warm=False) a full search. p95 is on the work-size valid pixels."""
    if like is None:
        return fit_gradient(small, valid, work=max(small.shape[:2]), size=size)   # `small` is at work size already
    data = _Data(small, valid, size)
    if len(data.x) < 16:
        return None, math.inf
    n, offs = len(like.stops), (np.array([s.offset for s in like.stops]) if warm else None)
    if like.kind == "linear":
        return fit_linear(None, None, data=data, n_stops=n, offsets=offs, angles=like.angle + WARM if warm else None)
    return fit_radial(None, None, data=data, n_stops=n, offsets=offs, center=like.center if warm else None)


def flip(g: Gradient) -> Gradient:
    """The same linear gradient read the other way round."""
    stops = [GradientStop(offset=round(1 - s.offset, 4), color=s.color) for s in g.stops[::-1]]
    return g.model_copy(update={"angle": (g.angle + 180) % 360, "stops": stops})


def follow(g: Gradient | None, prev: Gradient | None) -> Gradient | None:
    """g as the continuation of prev: a linear gradient read the other way round is flipped and its angle
    unwrapped next to prev's, so interpolating between the two turns the short way."""
    if g is None or prev is None or g.kind != "linear" or prev.kind != "linear":
        return g
    d = (g.angle - prev.angle + 180) % 360 - 180
    if abs(d) > 90:
        g = flip(g)
        d = (g.angle - prev.angle + 180) % 360 - 180
    return g.model_copy(update={"angle": round(prev.angle + d, 3)})
