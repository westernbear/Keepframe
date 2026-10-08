"""Textures v2: matted element textures. A binary-masked crop of one frame carries the background at its edges
(and whatever else sat in the mask), so a background or colour edit shows halos and cut-outs. Here each texture
solves I = αF + (1 − α)B on the element's best frames, warped into texture space with its own transform, against
the known per-frame plate B (`PlateModel.at(f)`) with the layers drawn below the element composited in:

- frames are filtered (on screen, not under a layer above, opaque, revealed), scored (contrast × sharpness × slow
  motion), aligned to the best one (sub-pixel ECC), and dropped when something unmodelled covers the element;
- the mask is re-decided against the per-frame plate: pixels no frame tells apart from the plate leave it (it never
  grows: what the region left out stays out);
- triangulation, per pixel, where B varies behind the element over ≥ 6 frames (it moves over a gradient or a
  picture, or the plate animates): I = G + kB, α = 1 − k, F = G / α;
- the two-colour model for flat glyphs and shapes: α the projection of I − B on F − B, F the interior colour;
- a keyed band otherwise: α the projection on the nearest solid colour. Inside, α = 1 and F is the reference
  frame's colour (the canonical frame, or the best one when the others outvote it), the per-pixel median where that
  frame strays; a band pixel neither model explains keeps its binary edge.

Per-frame estimates fuse by weighted median, F is extended under transparent pixels (no fringe after an edit),
and the texture grows by `pad` px a side so soft edges fit, its centre and anchor fixed. On a picture plate, the
filled-in (never seen) plate pixels are left out of triangulation and of the mask decision; the per-frame models
still use them, as the best estimate there is."""
from __future__ import annotations
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, NamedTuple, Sequence
import cv2, numpy as np
from ..ir.colour import delta_e, srgb_to_lab
from ..ir.gradient import gradient_at, render_gradient
from ..ir.schema import DEFAULTS, PROPS, TextureMeta
from ..ir.tracks import affine_matrix
from ..log import get
from .keyframes import fill_gaps

log = get("keepframe.analyze")

PAD = 3                     # px a side the texture grows, for soft edges
TOP_K = 8                   # frames fused by the per-frame models
VISIBLE_MIN = 0.98          # share of the element inside the frame …
OCCLUDED_MAX = 0.02         # … under a layer drawn above it …
OPACITY_MIN = 0.95          # … its opacity …
REVEAL_MIN = 0.999          # … and reveal, for a frame to count (relaxed in this order when none does)
RELAXED_CAP = 0.4           # confidence ceiling once a filter was relaxed
SPEED_PX = 6.0              # px/frame at which the motion term halves a frame's score
TRI_MIN = 6                 # frames a pixel needs for triangulation …
TRI_VAR = 12.0 ** 2         # … and this weighted variance of B (sRGB levels², summed over channels) …
TRI_RESIDUAL = 8.0          # … and an RMS line-fit residual below this (frames that disagree: misregistration, change)
FLAT_P90 = 6.0              # ΔE76 p90 of the interior around its median colour for the two-colour model …
EDGE_DE = 15.0              # … and ΔE76 between that colour and the plate …
EDGE_SHARE = 0.8            # … at this share of the edge pixels
INTERIOR_K = 0.35           # interior = mask eroded by max(1, this × stroke width)
FG_DE = 12.0                # ΔE76 from B that makes a pixel the element's in a frame …
EVIDENCE_SHARE = 0.5        # … in this share of the (weighted) frames, for it to stay in the mask
OUTLIER_DE = 20.0           # a solid pixel this far from its median colour is covered by something unmodelled …
OUTLIER_SHARE = 0.1         # … and a frame with more of them than this is dropped
ODD_DE = 15.0               # a band pixel the fitted model misses by this much keeps its binary edge
AGREE_SHARE = 0.3           # a frame whose core differs from the canonical frame's (ΔE > 12) here is dropped
CONSENSUS_MIN = 3           # agreeing frames that can outvote the canonical frame (R31)
BAND = 3                    # px around the mask where α is solved
CORE_PX = 2                 # keyed: the mask eroded by this is solid
EXTEND_THR = 0.05           # F under α below this is extended from its neighbours
F_TRUST = 0.25              # a triangulated F = G/α is used from this α up, else extended
SNAP = 0.02                 # triangulated α within this of 0 or 1 snaps there
COVER_A = 0.02              # a layer above hides the element where its α exceeds this
SEEN_MIN = 0.5              # share of the mask the chosen frames must show, else the binary texture stays
ALIGN_CC, ALIGN_PX = 0.8, 1.5   # a frame's sub-pixel alignment is kept above this correlation, within this shift
METHOD_FACTOR = {"triangulation": 1.0, "two_colour": 0.95, "keyed": 0.8, "binary": 0.3}
MIN_CANDIDATES, MAX_CANDIDATES = 12, 32
CANDIDATE_BYTES = 32 << 20  # warped frames + plates kept per element while scoring
_K3 = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
_FLAGS = cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP


class NotMatted(ValueError):
    """The element cannot be matted (no usable frame, or hidden in all of them): it keeps its binary texture."""


class Layer(NamedTuple):
    prem: np.ndarray        # h×w×4 float32 premultiplied RGBA, 0..1
    M: np.ndarray           # 2×3 (or 3×3) texture → scene
    opacity: float
    above: bool             # drawn over the element (hides it) or under it (part of what is behind it)
    alpha: np.ndarray | None = None   # h×w uint8 α of `prem`, if at hand (a layer above needs only that)


@dataclass(eq=False)
class Sample:
    frame: int
    I: np.ndarray           # th×tw×3 uint8: the frame in texture space
    B: np.ndarray           # th×tw×3 uint8: the plate there, with the layers below composited in
    valid: np.ndarray       # th×tw bool: inside the frame, not under a layer above, revealed
    own: np.ndarray         # th×tw bool: the element's mask (padded)
    others: np.ndarray      # th×tw bool: pixels a layer above covers
    score: float = 0.0
    visibility: float = 1.0
    occlusion: float = 0.0
    opacity: float = 1.0
    reveal: float = 1.0
    contrast: float = 0.0
    sharpness: float = 0.0
    speed: float = 0.0
    synthetic: np.ndarray | None = None   # th×tw bool: the plate is filled in here (never seen)


class _Still:
    """A plain image as a plate."""
    gradient_keys: list = []
    frames = None

    def __init__(self, image, synthetic=None):
        self.image, self.synthetic = np.asarray(image), synthetic

    def at(self, f: int) -> np.ndarray:
        return self.image


def as_plate(plate):
    return plate if hasattr(plate, "at") else _Still(plate)


def _h3(M) -> np.ndarray:
    M = np.asarray(M, np.float64)
    return M if M.shape == (3, 3) else np.vstack([M, [0.0, 0.0, 1.0]])


def _col(row, i: int, default: float) -> float:
    return float(row[i]) if len(row) > i and np.isfinite(row[i]) else default


def element_affine(raw_row, tex_shape, canon_wh, anchor=(0.5, 0.5)) -> np.ndarray:
    """2×3 texture pixel → scene, as `composite.texture_to_scene_affine` places the element."""
    p = dict(DEFAULTS)
    for i, name in enumerate(PROPS[:len(raw_row)]):
        if np.isfinite(raw_row[i]):
            p[name] = float(raw_row[i])
    th, tw = tex_shape
    cw, ch = canon_wh
    ax, ay = anchor
    L = np.array([[cw / tw, 0.0, -ax * cw], [0.0, ch / th, -ay * ch], [0.0, 0.0, 1.0]])
    return (affine_matrix(p) @ L)[:2]


def _disc(r: int) -> np.ndarray:
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))


def _grow(m: np.ndarray, r: int) -> np.ndarray:
    return cv2.dilate(m.astype(np.uint8), _disc(r)) > 0 if r > 0 else m.astype(bool)


def _erode(m: np.ndarray, r: int) -> np.ndarray:
    return cv2.erode(m.astype(np.uint8), _disc(r), borderType=cv2.BORDER_CONSTANT, borderValue=0) > 0 if r > 0 else m.astype(bool)


def _stroke_px(own: np.ndarray) -> float:
    """Stroke width: twice the p90 distance to the mask edge. The texture border counts as outside (an unpadded
    text texture's mask can touch it, or fill the whole box), and the width never exceeds the box."""
    dt = cv2.distanceTransform(np.pad(own, 1).astype(np.uint8), cv2.DIST_L2, 3)[1:-1, 1:-1]
    v = dt[own]
    return min(2.0 * float(np.percentile(v, 90)), float(min(own.shape))) if v.size else 1.0


def _whole_pixel(M: np.ndarray, corners: np.ndarray) -> np.ndarray:
    """A transform within half a pixel of a plain translation (scale and rotation noise of a held element) becomes
    a whole-pixel crop: positions are measured to about half a pixel (box centres), and bilinear resampling at a
    half pixel averages neighbours, which blurs every frame fused into the texture."""
    if np.abs(M[:, :2] - np.eye(2)).max() > 0.05:
        return M
    c = M @ corners
    t = np.floor(c.mean(1) - corners[:2].mean(1) + 0.5)
    if np.abs(c - (corners[:2] + t[:, None])).max() > 0.5 + 1e-9:
        return M
    return np.array([[1.0, 0.0, t[0]], [0.0, 1.0, t[1]]])


def _plate_B(plate, f: int, M: np.ndarray, ii: np.ndarray, jj: np.ndarray) -> np.ndarray:
    th, tw = ii.shape
    keys = getattr(plate, "gradient_keys", None)
    if getattr(plate, "frames", None) is None and keys and len(keys) >= 2:   # animated gradient: render it here
        H, W = plate.image.shape[:2]
        sx = M[0, 0] * ii + M[0, 1] * jj + M[0, 2]
        sy = M[1, 0] * ii + M[1, 1] * jj + M[1, 2]
        return render_gradient(gradient_at(plate, f), W, H, xy=(sx + 0.5, sy + 0.5))
    return cv2.warpAffine(plate.at(f), M, (tw, th), flags=_FLAGS, borderMode=cv2.BORDER_REPLICATE)


def gather_samples(raw, canon_binary, frames, plate, others_at: Callable[[int], Sequence[Layer]] | None = None, pad: int = PAD,
                   *, candidates: Sequence[int] | None = None, offsets: dict | None = None) -> list[Sample]:
    """Every frame with a measured transform (evenly thinned to a memory budget), warped into the padded texture
    space with the plate behind it; layers below the element are composited into B, layers above mark `others`.
    `offsets` {frame: (dx, dy)} shift a frame's texture coordinates (sub-pixel alignment to a reference frame)."""
    plate = as_plate(plate)
    own = np.pad(np.asarray(canon_binary, bool), pad)
    th, tw = own.shape
    n, H, W = frames.shape[:3]
    last = min(len(raw), n)
    pool = range(last) if candidates is None else candidates
    rows = [int(f) for f in pool if 0 <= f < last and np.isfinite(raw[f, :2]).all()]
    cap = int(np.clip(CANDIDATE_BYTES // max(1, th * tw * 8), MIN_CANDIDATES, MAX_CANDIDATES))
    if len(rows) > cap:
        rows = [rows[i] for i in np.unique(np.rint(np.linspace(0, len(rows) - 1, cap)).astype(int))]
    jj, ii = np.mgrid[0:th, 0:tw].astype(np.float32)
    # Filled-in plate pixels are estimates on a picture plate; a fitted gradient (or flat colour) holds there too.
    syn = getattr(plate, "synthetic", None) if getattr(plate, "kind", "image") == "image" else None
    syn8 = syn.astype(np.uint8) * 255 if syn is not None and syn.any() else None
    own_n = max(1, int(own.sum()))
    near = _grow(own, 2)
    corners = np.array([[0, 0, 1], [tw - 1, 0, 1], [0, th - 1, 1], [tw - 1, th - 1, 1]], np.float64).T
    out = []
    for f in rows:
        M = element_affine(raw[f], (th, tw), (tw, th))
        if offsets and f in offsets:
            M = M @ np.array([[1.0, 0.0, offsets[f][0]], [0.0, 1.0, offsets[f][1]], [0.0, 0.0, 1.0]])
        M = _whole_pixel(M, corners)
        I = cv2.warpAffine(frames[f], M, (tw, th), flags=_FLAGS, borderMode=cv2.BORDER_REPLICATE)
        B = _plate_B(plate, f, M, ii, jj)
        c = M @ corners
        if c[0].min() >= 0 and c[0].max() <= W - 1 and c[1].min() >= 0 and c[1].max() <= H - 1:
            inside = np.ones((th, tw), bool)
        else:
            sx = M[0, 0] * ii + M[0, 1] * jj + M[0, 2]
            sy = M[1, 0] * ii + M[1, 1] * jj + M[1, 2]
            inside = (sx >= 0) & (sx <= W - 1) & (sy >= 0) & (sy <= H - 1)
        others = np.zeros((th, tw), bool)
        layers = others_at(f) if others_at is not None else ()
        if layers:
            try:
                inv = np.linalg.inv(_h3(M))
            except np.linalg.LinAlgError:
                inv = None
            Bf = None
            for L in layers if inv is not None else ():
                A = (inv @ _h3(L.M))[:2]
                lh, lw = L.prem.shape[:2]
                c = A @ np.array([[-1, -1, 1], [lw, -1, 1], [-1, lh, 1], [lw, lh, 1]], np.float64).T   # its reach here
                x0, y0 = max(0, int(np.floor(c[0].min()))), max(0, int(np.floor(c[1].min())))
                x1, y1 = min(tw, int(np.ceil(c[0].max())) + 1), min(th, int(np.ceil(c[1].max())) + 1)
                if x1 <= x0 or y1 <= y0:
                    continue
                A[:, 2] -= (x0, y0)
                if L.above:
                    a8 = L.alpha if L.alpha is not None else np.rint(L.prem[..., 3] * 255).astype(np.uint8)
                    a = cv2.warpAffine(a8, A, (x1 - x0, y1 - y0), flags=cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
                    others[y0:y1, x0:x1] |= a > COVER_A * 255 / max(L.opacity, 1e-6)   # nearest, then grown by 1 px below
                    continue
                w = cv2.warpAffine(L.prem, A, (x1 - x0, y1 - y0), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
                a = w[..., 3:4] * np.float32(L.opacity)
                if not (a > COVER_A).any():
                    continue
                Bf = B.astype(np.float32) if Bf is None else Bf
                roi = Bf[y0:y1, x0:x1]
                roi *= 1 - a
                roi += w[..., :3] * np.float32(255 * L.opacity)
            if Bf is not None:
                B = np.clip(np.rint(Bf), 0, 255).astype(np.uint8)
            if others.any():
                others = cv2.dilate(others.astype(np.uint8), _K3) > 0
        opacity, reveal = _col(raw[f], 7, 1.0), _col(raw[f], 8, 1.0)
        valid = inside & ~others
        if reveal < REVEAL_MIN:   # the compositor clips the texture's columns past the reveal
            valid[:, pad + int(round((tw - 2 * pad) * max(reveal, 0.0))):] = False
        m = own & valid
        nm = int(m.sum())
        contrast = cv2.norm(I, B, cv2.NORM_L1, mask=m.astype(np.uint8)) / nm if nm else 0.0   # L1 over the channels
        lap = cv2.Laplacian(cv2.cvtColor(I, cv2.COLOR_RGB2GRAY), cv2.CV_32F)
        r = near & valid
        sharp = float(lap[r].var()) if r.sum() > 1 else 0.0
        synthetic = (cv2.warpAffine(syn8, M, (tw, th), flags=_FLAGS, borderMode=cv2.BORDER_CONSTANT, borderValue=0) > 0
                     if syn8 is not None else None)
        out.append(Sample(f, I, B, valid, own, others, visibility=float((own & inside).sum()) / own_n,
                          occlusion=float((own & others).sum()) / own_n, opacity=opacity, reveal=reveal,
                          contrast=contrast, sharpness=sharp, synthetic=synthetic))
    return out


def _speed(raw, f: int, shape) -> float:
    """Largest texture-corner displacement per frame around f (central difference where it can)."""
    th, tw = shape
    C = np.array([[0, 0, 1], [tw - 1, 0, 1], [0, th - 1, 1], [tw - 1, th - 1, 1]], np.float64).T

    def at(g):
        return element_affine(raw[g], shape, (tw, th)) @ C if 0 <= g < len(raw) and np.isfinite(raw[g, :2]).all() else None

    a, c, b = at(f - 1), at(f), at(f + 1)
    if a is not None and b is not None:
        d = (b - a) / 2
    elif a is not None or b is not None:
        d = (c - a) if a is not None else (b - c)
    else:
        return 0.0
    return float(np.hypot(d[0], d[1]).max())


_FILTERS = (("visibility", lambda s: s.visibility >= VISIBLE_MIN), ("occlusion", lambda s: s.occlusion <= OCCLUDED_MAX),
            ("opacity", lambda s: s.opacity >= OPACITY_MIN), ("reveal", lambda s: s.reveal >= REVEAL_MIN))


def score_samples(samples: list[Sample], raw, top_k: int = TOP_K) -> tuple[list[Sample], list[str]]:
    """Score = contrast_norm × sharpness_norm × 1/(1 + (speed/6)²) over the frames that pass the filters (relaxed in
    order — visibility, occlusion, opacity, reveal — while none does). Every passing sample keeps its score (> 0;
    the others 0). Returns the top `top_k` and the names of the relaxed filters."""
    for s in samples:
        s.score = 0.0
    if not samples:
        return [], []
    shape = samples[0].own.shape
    keep, relaxed = [], []
    for i in range(len(_FILTERS) + 1):
        keep = [s for s in samples if all(test(s) for _, test in _FILTERS[i:])]
        if keep:
            relaxed = [name for name, _ in _FILTERS[:i]]
            break
    if not keep:
        return [], [name for name, _ in _FILTERS]
    cmax = max(s.contrast for s in keep) or 1.0
    smax = max(s.sharpness for s in keep) or 1.0
    for s in keep:
        s.speed = _speed(raw, s.frame, shape)
        s.score = max(1e-6, (s.contrast / cmax) * (s.sharpness / smax) / (1.0 + (s.speed / SPEED_PX) ** 2))
    keep.sort(key=lambda s: (-s.score, s.frame))
    return keep[:top_k], relaxed


def triangulate(samples: list[Sample]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per pixel, the weighted line fit I = G + kB over the samples (weights = scores; synthetic plate pixels
    left out): α = clip(1 − k), F = G/α. `determined` where ≥ 6 samples, Σw‖B − B̄‖² ≥ 12² (w normalised) and
    the fit holds (RMS residual ≤ 8, k within [−0.1, 1.1])."""
    th, tw = samples[0].own.shape
    tot = np.zeros((th, tw), np.float32)
    cnt = np.zeros((th, tw), np.int32)
    Bm = np.zeros((th, tw, 3), np.float32)
    Im = np.zeros((th, tw, 3), np.float32)

    def weight(s):
        v = s.valid if s.synthetic is None else s.valid & ~s.synthetic
        return v, v.astype(np.float32) * np.float32(max(s.score, 1e-6))

    for s in samples:
        v, w = weight(s)
        tot += w
        cnt += v
        Bm += w[..., None] * s.B
        Im += w[..., None] * s.I
    norm = 1.0 / np.maximum(tot, 1e-12)
    Bm *= norm[..., None]
    Im *= norm[..., None]
    var = np.zeros((th, tw), np.float32)
    cov = np.zeros((th, tw), np.float32)
    for s in samples:
        _, w = weight(s)
        dB = s.B.astype(np.float32) - Bm
        var += w * (dB * dB).sum(-1)
        cov += w * (dB * (s.I.astype(np.float32) - Im)).sum(-1)
    var *= norm
    cov *= norm
    k = cov / np.maximum(var, 1e-6)
    res = np.zeros((th, tw), np.float32)
    for s in samples:
        _, w = weight(s)
        r = (s.I.astype(np.float32) - Im) - k[..., None] * (s.B.astype(np.float32) - Bm)
        res += w * (r * r).sum(-1)
    res *= norm
    alpha = np.clip(1.0 - k, 0.0, 1.0)
    alpha[alpha >= 1 - SNAP] = 1.0
    alpha[alpha <= SNAP] = 0.0
    G = Im - k[..., None] * Bm
    F = np.clip(G / np.maximum(alpha, 1e-3)[..., None], 0, 255)
    determined = (cnt >= TRI_MIN) & (var >= TRI_VAR) & (np.sqrt(res) <= TRI_RESIDUAL) & (k > -0.1) & (k < 1.1)
    return alpha.astype(np.float32), F.astype(np.float32), determined


def _defade(s: Sample) -> tuple[np.ndarray, np.ndarray, float]:
    """I and B as float, and the opacity to divide out (only for a frame below the opacity filter)."""
    I, B = s.I.astype(np.float32), s.B.astype(np.float32)
    o = max(s.opacity, 0.05) if s.opacity < OPACITY_MIN else 1.0
    return I, B, o


def two_colour(samples: list[Sample], fill_rgb) -> list[np.ndarray]:
    """Per sample, α = clip(((I − B)·(F − B)) / ‖F − B‖²) with F the flat fill; NaN where the sample is not valid or
    F is within ΔE 15 of B (nothing to project on)."""
    F = np.asarray(fill_rgb, np.float32)
    Flab = srgb_to_lab(F)
    out = []
    for s in samples:
        I, B, o = _defade(s)
        d = F - B
        a = np.clip(((I - B) * d).sum(-1) / np.maximum((d * d).sum(-1), 1e-6) / o, 0.0, 1.0)
        ok = s.valid.copy()
        ok[ok] = delta_e(srgb_to_lab(B[ok]), Flab) >= EDGE_DE
        out.append(np.where(ok, a, np.nan).astype(np.float32))
    return out


def _nearest(core: np.ndarray) -> np.ndarray:
    """Flat index of the nearest `core` pixel, per pixel."""
    _, labels = cv2.distanceTransformWithLabels((~core).astype(np.uint8), cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL)
    lut = np.zeros(int(labels.max()) + 1, np.int64)
    lut[labels[core]] = np.flatnonzero(core)
    return lut[labels]


def _core(own: np.ndarray) -> np.ndarray:
    for r in (CORE_PX, 1):
        c = _erode(own, r)
        if c.any():
            return c
    return own.copy()


def keyed(samples: list[Sample], own: np.ndarray | None = None) -> list[tuple[np.ndarray, np.ndarray]]:
    """Per sample (α, F): solid inside the mask eroded by 2 px (F = I); in the 3 px band around the mask edge α is
    the projection of I − B on F_near − B, F_near the colour of the nearest solid pixel; where F_near is within
    ΔE 15 of B the binary edge stays (α = mask, F = I). NaN α where the sample is not valid. `own`: the mask
    (default: the first sample's)."""
    own = samples[0].own if own is None else own
    core = _core(own)
    band = _grow(own, BAND) & ~core
    idx = _nearest(core)
    out = []
    for s in samples:
        I, B, o = _defade(s)
        Fi = np.clip((I - (1 - o) * B) / o, 0, 255) if o < 1 else I
        Fn = Fi.reshape(-1, 3)[idx].reshape(Fi.shape)
        d = Fn - B
        a = np.clip(((I - B) * d).sum(-1) / np.maximum((d * d).sum(-1), 1e-6) / o, 0.0, 1.0)
        amb = np.zeros_like(band)
        if band.any():
            amb[band] = delta_e(srgb_to_lab(Fn[band]), srgb_to_lab(B[band])) < EDGE_DE
        alpha = np.where(core, 1.0, np.where(band, np.where(amb, own.astype(np.float32), a), 0.0))
        F = np.where((core | (amb & own))[..., None], Fi, Fn)
        out.append((np.where(s.valid, alpha, np.nan).astype(np.float32), F.astype(np.float32)))
    return out


def _wmedian(v: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Weighted median along axis 0, NaN entries left out (NaN where none is left)."""
    nan = np.isnan(v)
    wb = np.where(nan, 0.0, np.broadcast_to(w.reshape(-1, *([1] * (v.ndim - 1))), v.shape)).astype(np.float64)
    vv = np.where(nan, np.inf, v)
    order = np.argsort(vv, axis=0, kind="stable")
    vs = np.take_along_axis(vv, order, 0)
    cw = np.cumsum(np.take_along_axis(wb, order, 0), 0)
    tot = cw[-1]
    idx = np.argmax(cw >= 0.5 * tot[None], axis=0)
    out = np.take_along_axis(vs, idx[None], 0)[0].astype(np.float32)
    out[tot <= 0] = np.nan
    return out


def fuse(alphas, Fs, weights) -> tuple[np.ndarray, np.ndarray | None]:
    """Weighted medians across samples: α, and F per channel (F only where that sample's α is known)."""
    w = np.asarray(weights, np.float64)
    A = np.stack(alphas).astype(np.float32)
    alpha = _wmedian(A, w)
    if Fs is None:
        return alpha, None
    Fst = np.where(np.isnan(A)[..., None], np.nan, np.stack(Fs).astype(np.float32))
    return alpha, _wmedian(Fst, w)


def _extend(F: np.ndarray, known: np.ndarray) -> np.ndarray:
    if known.all() or not known.any():
        return F
    out = F.reshape(-1, 3).copy()
    out[:] = out[_nearest(known).ravel()]
    return out.reshape(F.shape)


def extend_foreground(F: np.ndarray, alpha: np.ndarray, thr: float = EXTEND_THR) -> np.ndarray:
    """F under α < thr becomes the colour of the nearest pixel with α ≥ thr, so resampling and edits pull no
    black or background into the edge."""
    return _extend(np.asarray(F, np.float32), np.asarray(alpha) >= thr)


def _median_colour(top: list[Sample], w: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Per pixel of `mask`, the weighted median over the samples of I (with a faded frame's opacity divided out);
    NaN where no sample is valid. A frame something unmodelled crosses does not move it."""
    vals = []
    for s in top:
        I, B, o = _defade(s)
        Fi = np.clip((I - (1 - o) * B) / o, 0, 255) if o < 1 else I
        vals.append(np.where(s.valid[mask][:, None], Fi[mask], np.nan))
    return _wmedian(np.stack(vals).astype(np.float32), w)


def _core_colour(top: list[Sample], w: np.ndarray, core: np.ndarray, ref: Sample) -> tuple[np.ndarray, np.ndarray]:
    """F inside the solid core, and where it is known: the reference frame's colour (fusing frames that disagree by
    a fraction of a pixel blurs detail), the per-pixel median where that frame does not see the pixel or strays from
    it by ΔE > 20 (something unmodelled crossing that frame, too small to make it an outlier: R32)."""
    th, tw = core.shape
    F = np.full((th, tw, 3), np.nan, np.float32)
    if not core.any():
        return F, np.zeros((th, tw), bool)
    med = _median_colour(top, w, core)
    I, B, o = _defade(ref)
    best = np.clip((I - (1 - o) * B) / o, 0, 255)[core] if o < 1 else I[core]
    use = ref.valid[core] & ~np.isnan(med).any(-1)
    use[use] = delta_e(srgb_to_lab(best[use]), srgb_to_lab(med[use])) <= OUTLIER_DE
    vals = np.where(use[:, None], best, med)
    F[core] = vals
    known = np.zeros((th, tw), bool)
    known[core] = ~np.isnan(vals).any(-1)
    return F, known


def _unexplained(top: list[Sample], w: np.ndarray, alpha: np.ndarray, F: np.ndarray, band: np.ndarray) -> np.ndarray:
    """Band pixels where αF + (1 − α)B misses the frames by ΔE > 15 (weighted median over the samples)."""
    if not band.any():
        return band
    a, Fb = np.nan_to_num(alpha[band])[:, None], F[band]
    errs = []
    for s in top:
        e = np.full(int(band.sum()), np.nan, np.float32)
        m = s.valid[band]
        R = a[m] * Fb[m] + (1 - a[m]) * s.B[band][m]
        e[m] = delta_e(srgb_to_lab(R), srgb_to_lab(s.I[band][m].astype(np.float32)))
        errs.append(e)
    med = _wmedian(np.stack(errs), w)
    out = np.zeros_like(band)
    out[band] = np.nan_to_num(med) > ODD_DE
    return out


def _unmixed(top: list[Sample], w: np.ndarray, alpha: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Per pixel of `mask`, the weighted median over the samples of (I − (1 − α)B) / α (opacity divided out)."""
    a = alpha[mask][:, None]
    vals = []
    for s in top:
        I, B, o = _defade(s)
        ao = a * o
        Fu = np.clip((I[mask] - (1 - ao) * B[mask]) / np.maximum(ao, 1e-3), 0, 255)
        vals.append(np.where(s.valid[mask][:, None], Fu, np.nan))
    return _wmedian(np.stack(vals).astype(np.float32), w)


def _flat_fill(top: list[Sample], w: np.ndarray, interior: np.ndarray, own: np.ndarray) -> tuple[np.ndarray | None, bool]:
    """The interior's median colour, and whether the two-colour model applies: the per-pixel medians of the
    interior within ΔE 6 of it at p90, and the plate ≥ ΔE 15 from it at ≥ 80 % of the edge pixels."""
    if interior.sum() < 8:
        return None, False
    px = _median_colour(top, w, interior)
    px = px[~np.isnan(px).any(-1)]
    edge = own & ~_erode(own, 1)
    Bs = np.concatenate([s.B[edge & s.valid] for s in top]).astype(np.float32)
    if len(px) < 8 or not len(Bs):
        return None, False
    fill = np.median(px, 0).astype(np.float32)
    flab = srgb_to_lab(fill)
    flat = float(np.percentile(delta_e(srgb_to_lab(px), flab), 90)) <= FLAT_P90
    step = max(1, len(Bs) // 20000)
    share = float((delta_e(srgb_to_lab(Bs[::step]), flab) >= EDGE_DE).mean())
    return fill, flat and share >= EDGE_SHARE


def _agree(ranked: list[Sample], ref: Sample, core: np.ndarray) -> list[Sample]:
    """The frames whose solid core matches the canonical frame's: more than 30 % of the core pixels (seen in both)
    over ΔE 12 from it means the transform followed something else, or something covers the element. The
    canonical frame always stays; with no other frame agreeing, it is the texture on its own."""
    out = []
    for s in ranked:
        if s is not ref:
            m = core & s.valid & ref.valid
            if m.sum() >= 8:
                de = delta_e(srgb_to_lab(s.I[m].astype(np.float32)), srgb_to_lab(ref.I[m].astype(np.float32)))
                if float((de > FG_DE).mean()) > AGREE_SHARE:
                    continue
        out.append(s)
    return out


def _consensus(rest: list[Sample], cf: Sample, core: np.ndarray) -> list[Sample] | None:
    """The other frames, when they outvote the canonical frame: they agree among themselves (a majority within
    ΔE 12 of their per-pixel median at ≤ 30 % of the core pixels) and cf does not (> 30 %). Returns those agreeing
    frames, else None (cf stands: frames a tracker mis-placed scatter and disagree with each other too). A consensus
    needs at least 3 agreeing frames: one or two frames always agree with their own median."""
    if len(rest) < CONSENSUS_MIN or core.sum() < 8:
        return None
    ncore = int(core.sum())

    def lab(s):
        out = np.full((ncore, 3), np.nan, np.float32)
        m = s.valid[core]
        out[m] = srgb_to_lab(s.I[core][m].astype(np.float32))
        return out

    labs = np.stack([lab(s) for s in rest])
    med = _wmedian(labs, np.array([s.score for s in rest], np.float64))

    def share(l):
        m = ~np.isnan(l[:, 0]) & ~np.isnan(med[:, 0])
        return float((np.linalg.norm(l[m] - med[m], axis=-1) > FG_DE).mean()) if m.sum() >= 8 else None

    agreeing = [s for s, l in zip(rest, labs) if (sh := share(l)) is not None and sh <= AGREE_SHARE]
    if 2 * len(agreeing) <= len(rest) or len(agreeing) < CONSENSUS_MIN:
        return None
    cf_share = share(lab(cf))
    return agreeing if cf_share is not None and cf_share > AGREE_SHARE else None


def _consistent(ranked: list[Sample], core: np.ndarray) -> list[Sample]:
    """Drops frames in which something no layer models (a sprite the tracker lost, a flash) covers the element:
    those whose share of solid pixels more than ΔE 20 from the per-pixel median exceeds 10 % and twice the median
    share. Keeps at least half the frames."""
    if len(ranked) < 3 or core.sum() < 8:
        return ranked
    w = np.array([s.score for s in ranked], np.float64)
    labs = []
    for s in ranked:
        lab = np.full((int(core.sum()), 3), np.nan, np.float32)
        m = s.valid[core]
        lab[m] = srgb_to_lab(s.I[core][m].astype(np.float32))
        labs.append(lab)
    labs = np.stack(labs)
    med = _wmedian(labs, w)
    share = np.array([float(np.nanmean(np.where(np.isnan(l[:, 0]) | np.isnan(med[:, 0]), np.nan,
                                                np.linalg.norm(l - med, axis=-1) > OUTLIER_DE)))
                      if not np.isnan(l[:, 0]).all() else 1.0 for l in labs])
    share = np.nan_to_num(share, nan=1.0)
    keep = share <= max(OUTLIER_SHARE, 2 * float(np.median(share)))
    if keep.sum() < (len(ranked) + 1) // 2:
        keep = share <= np.sort(share)[(len(ranked) + 1) // 2 - 1]
    return [s for s, k in zip(ranked, keep) if k]


def _evidence(top: list[Sample], w: np.ndarray, near: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(fg, judged): pixels near the mask that differ from B by ΔE > 12 in at least half the weighted samples
    where B was seen (not filled in), and the pixels where it was. A mask cut against an older plate keeps plate
    pixels that no frame shows apart from the plate; where the plate is only an estimate, nothing is judged."""
    yes = np.zeros(near.shape, np.float32)
    seen = np.zeros(near.shape, np.float32)
    for s, ws in zip(top, w):
        m = near & s.valid & (True if s.synthetic is None else ~s.synthetic)
        if not m.any():
            continue
        fg = delta_e(srgb_to_lab(s.I[m].astype(np.float32)), srgb_to_lab(s.B[m].astype(np.float32))) > FG_DE
        seen[m] += ws
        yes[m] += ws * fg
    judged = seen > 0
    return judged & (yes >= EVIDENCE_SHARE * seen), judged


def _offsets(ranked: list[Sample], ref: Sample, canon: np.ndarray) -> dict:
    """Sub-pixel translation of each frame onto the reference frame (ECC on luminance around the mask): the
    measured transforms are good to about half a pixel, and fusing frames that disagree by that blurs the texture.
    Kept when the fit correlates (≥ 0.8) and stays within 1.5 px."""
    mask = (_grow(canon, 1) & ref.valid).astype(np.uint8)
    if mask.sum() < 16:
        return {}
    grey = lambda s: cv2.cvtColor(s.I, cv2.COLOR_RGB2GRAY).astype(np.float32)
    tpl = grey(ref)
    crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 1e-3)
    out = {}
    for s in ranked:
        if s is ref:
            continue
        try:
            cc, Wm = cv2.findTransformECC(tpl, grey(s), np.eye(2, 3, dtype=np.float32), cv2.MOTION_TRANSLATION, crit, mask, 1)
        except cv2.error:
            continue
        t = Wm[:, 2].astype(np.float64)
        if cc >= ALIGN_CC and np.abs(t).max() <= ALIGN_PX and np.abs(t).max() >= 0.05:
            out[s.frame] = (float(t[0]), float(t[1]))
    return out


def texture_v2(raw, canon_binary, frames, plate, *, others_at=None, pad: int = PAD, top_k: int = TOP_K, kind_hint: str | None = None,
               candidates: Sequence[int] | None = None, keep: tuple[np.ndarray, np.ndarray] | None = None,
               ref_frame: int | None = None) -> tuple[np.ndarray, TextureMeta]:
    """The matted, padded RGBA texture (uint8, straight colour) and its TextureMeta. `kind_hint` "mover": its mask
    (Task 5, noise-adaptive) stays and nothing is triangulated (an element animating in place changes against the
    plate on its own). `candidates` limits the frames; `keep` = (mask, rgb) in canonical space stays opaque with that
    colour (a mover's filled-in pixels). `ref_frame`, the canonical frame (where the binary texture came from and the
    transform is exact by construction), anchors the texture when it passes the filters: frames align to it, frames
    whose core disagrees with it are dropped, and the solid colour is its own — unless the other frames agree among
    themselves and cf does not (something unmodelled sits in cf), when their consensus wins. Raises NotMatted when
    no frame shows enough of it."""
    plate = as_plate(plate)
    samples = gather_samples(raw, canon_binary, frames, plate, others_at, pad, candidates=candidates)
    if ref_frame is not None and all(s.frame != ref_frame for s in samples) and (candidates is None or ref_frame in candidates):
        samples += gather_samples(raw, canon_binary, frames, plate, others_at, pad, candidates=[ref_frame])   # thinned out
    top, relaxed = score_samples(samples, raw, top_k)
    if not top:
        raise NotMatted("no frame shows it")
    canon = top[0].own
    if (np.any([s.valid for s in top], axis=0) & canon).sum() < SEEN_MIN * canon.sum():
        raise NotMatted("other layers cover it (or it is off-frame) in every usable frame")
    ranked = sorted((s for s in samples if s.score > 0), key=lambda s: (-s.score, s.frame))
    cf = next((s for s in ranked if s.frame == ref_frame), None)   # the canonical frame, when it passes the filters
    if cf is not None:
        consensus = _consensus([s for s in ranked if s is not cf], cf, _core(canon))
        if consensus is not None:   # R31: cf is the outlier (something unmodelled in it); the agreeing frames win
            cf, ranked = None, consensus
    ref = cf or ranked[0]
    offsets = _offsets(ranked, ref, canon)   # onto the reference frame
    if offsets:   # re-warp those frames aligned to the reference; they keep their scores
        moved = {s.frame: s for s in gather_samples(raw, canon_binary, frames, plate, others_at, pad, candidates=sorted(offsets),
                                                    offsets=offsets)}
        for i, s in enumerate(ranked):
            m = moved.get(s.frame)
            if m is not None:
                m.score, m.speed = s.score, s.speed
                ranked[i] = m
    if cf is not None:
        ranked = _agree(ranked, cf, _core(canon))
        top = [cf] + [s for s in ranked if s is not cf][:top_k - 1]
    else:
        ranked = _consistent(ranked, _core(canon))
        ref = ranked[0]
        top = ranked[:top_k]
    th, tw = canon.shape
    w = np.array([s.score for s in top], np.float64)
    # A mover's mask (Task 5) follows the measured noise, below ΔE 12: keep it. Other masks were cut against the
    # pass-1 plate or one frame; the per-frame plate re-decides them.
    if kind_hint == "mover":
        own = canon.copy()
    else:
        # The mask only loses pixels: one that the region left out (another palette colour, an unmodelled layer
        # crossing it) stays out; the band around the mask still solves the soft edge.
        fg, judged = _evidence(top, w, canon)
        own = canon & (fg | ~judged)
    if not own.any():
        own = canon.copy()
    for s in (*samples, *ranked):
        s.own = own
    region = _grow(own, BAND)
    interior = _erode(own, max(1, int(round(INTERIOR_K * _stroke_px(own)))))
    fill, flat = _flat_fill(top, w, interior, own)
    core = _core(own)
    F_core, solid = _core_colour(top, w, core, ref)
    if flat:   # α from the projection on the fill, F the fill; solid inside, where F keeps its own detail
        method = "two_colour"
        alpha, _ = fuse(two_colour(top, fill), None, w)
        F = np.empty((th, tw, 3), np.float32)
        F[:] = fill
    else:      # α from the projection on the nearest solid colour; F unmixed from the frames where α ≥ 0.5, else that colour
        method = "keyed"
        alpha, _ = fuse([a for a, _ in keyed(top, own)], None, w)
        F = np.full((th, tw, 3), np.nan, np.float32)
        rim = region & ~solid & (np.nan_to_num(alpha) >= 0.5)
        if rim.any():
            F[rim] = _unmixed(top, w, alpha, rim)
    alpha[solid] = 1.0
    F[solid] = F_core[solid]
    if np.isnan(F).any():
        F = _extend(np.nan_to_num(F), ~np.isnan(F).any(-1)) if solid.any() else np.nan_to_num(F)
    odd = _unexplained(top, w, alpha, F, region & ~solid)
    if odd.any():   # a third colour the model cannot explain (a detail on the edge): the binary edge, today's colour
        med = _median_colour(top, w, odd)
        ok = ~np.isnan(med).any(-1)
        oy, ox = np.nonzero(odd)
        alpha[oy, ox] = np.where(own[oy, ox], 1.0, 0.0)
        F[oy[ok], ox[ok]] = med[ok]
    seen = np.any([s.valid for s in top], axis=0)
    gap = np.isnan(alpha)
    alpha = np.where(gap, np.where(seen, own, 0.0), alpha).astype(np.float32)   # never seen (others above): α 0
    trusted = np.ones((th, tw), bool)
    tri = kind_hint != "mover" and len(ranked) >= TRI_MIN
    used = {s.frame for s in top}
    if tri:
        ta, tF, det = triangulate(ranked)
        det &= _grow(own | canon, BAND)
        if det.any():
            alpha = np.where(det, ta, alpha)
            F = np.where(det[..., None], tF, F)
            trusted &= ~(det & (ta < F_TRUST))
            used |= {s.frame for s in ranked}
        edge = region & ~_erode(own, CORE_PX)
        if edge.any() and det[edge].mean() >= 0.5:
            method = "triangulation"
        reach = region | det
    else:
        reach = region
    alpha[~reach] = 0.0
    if keep is not None:
        km = np.pad(np.asarray(keep[0], bool), pad)
        alpha[km] = 1.0
        F[km] = np.pad(np.asarray(keep[1], np.float32), ((pad, pad), (pad, pad), (0, 0)))[km]
    F = _extend(F, (alpha >= EXTEND_THR) & trusted)
    mean_de = _fit_de(top, w, alpha, F, region)
    conf = float(np.clip(1.0 - mean_de / 10.0, 0.0, 1.0)) * METHOD_FACTOR[method]
    if relaxed:
        conf = min(conf, RELAXED_CAP)
    rgba = np.dstack([np.clip(np.rint(F), 0, 255), np.clip(np.rint(alpha * 255), 0, 255)]).astype(np.uint8)
    return rgba, TextureMeta(method=method, frames=sorted(used), confidence=round(conf, 3), padding=pad)


def _fit_de(top: list[Sample], w: np.ndarray, alpha: np.ndarray, F: np.ndarray, region: np.ndarray) -> float:
    """Weighted mean ΔE76 between the frames and αF + (1 − α)B over `region` (10 when nothing is measured)."""
    errs, ws = [], []
    for s, sw in zip(top, w):
        m = region & s.valid
        if not m.any():
            continue
        a = alpha[m, None] * (min(1.0, s.opacity) if s.opacity < OPACITY_MIN else 1.0)
        R = a * F[m] + (1 - a) * s.B[m]
        errs.append(float(delta_e(srgb_to_lab(R), srgb_to_lab(s.I[m].astype(np.float32))).mean()))
        ws.append(sw)
    return float(np.average(errs, weights=ws)) if errs else 10.0


def pad_reveal(raw: np.ndarray, width: int, pad: int = PAD) -> None:
    """Map a reveal column (fraction of the canonical width) onto the padded texture, so the clip edge stays at
    the same pixel: r' = (p + r·w) / (w + 2p) for r < 1. In place."""
    if raw.shape[1] <= 8:
        return
    r = raw[:, 8]
    m = np.isfinite(r) & (r < 1.0)
    raw[m, 8] = (pad + r[m] * width) / (width + 2 * pad)


# --- the pipeline phase ------------------------------------------------------------------------------------------

def _premultiplied(canon: np.ndarray) -> np.ndarray:
    prem = canon.astype(np.float32) / 255
    prem[..., :3] *= prem[..., 3:4]
    return prem


def _box(M: np.ndarray, tw: int, th: int) -> tuple[float, float, float, float]:
    c = M @ np.array([[-1, -1, 1], [tw, -1, 1], [-1, th, 1], [tw, th, 1]], np.float64).T
    return c[0].min(), c[1].min(), c[0].max(), c[1].max()


def _seen_alpha(p: dict, frames: np.ndarray, plate) -> np.ndarray:
    """The binary α of an element where its canonical frame differs from the plate (ΔE > 12): what it can hide.
    A region cut against the pass-1 plate can be mostly plate (a halo ghost), and must not hide the others."""
    canon, raw = p["canon"], p["raw"]
    th, tw = canon.shape[:2]
    rows = np.flatnonzero(np.isfinite(raw[:, 0]) & np.isfinite(raw[:, 1]))
    if not len(rows):
        return np.ascontiguousarray(canon[..., 3])
    f = int(rows[np.argmin(np.abs(rows - int(p.get("cf", rows[0]))))])
    M = element_affine(raw[f], (th, tw), (tw, th))
    jj, ii = np.mgrid[0:th, 0:tw].astype(np.float32)
    I = cv2.warpAffine(frames[f], M, (tw, th), flags=_FLAGS, borderMode=cv2.BORDER_REPLICATE)
    B = _plate_B(plate, f, M, ii, jj)
    on = canon[..., 3] > 127
    fg = np.zeros((th, tw), bool)
    fg[on] = delta_e(srgb_to_lab(I[on].astype(np.float32)), srgb_to_lab(B[on].astype(np.float32))) > FG_DE
    return np.where(_grow(fg, 1), canon[..., 3], 0).astype(np.uint8)


def _layers(props: dict, keys: list[str], frames: np.ndarray | None = None, plate=None):
    """Every element's binary texture placed at every frame it is drawn (gap-filled raw), in render order (z, then
    first frame and mean x, as `_elements_from_props` orders them). Returns others_for(key) → others_at(f). With
    `frames` and `plate`, a layer above hides only where its canonical frame differs from the plate."""
    order = sorted(keys, key=lambda k: (props[k]["first"], float(np.nanmean(props[k]["raw"][:, 0]))))
    order.sort(key=lambda k: props[k].get("z", 0))
    rank = {k: i for i, k in enumerate(order)}
    placed = {}
    for k in keys:
        canon = props[k]["canon"]
        th, tw = canon.shape[:2]
        raw = fill_gaps(props[k]["raw"])
        per = {}
        for f in np.flatnonzero(np.isfinite(raw[:, 0]) & np.isfinite(raw[:, 1])):
            M = element_affine(raw[f], (th, tw), (tw, th))
            per[int(f)] = (M, _box(M, tw, th), float(np.clip(_col(raw[f], 7, 1.0), 0, 1)), _col(raw[f], 8, 1.0))
        a8 = _seen_alpha(props[k], frames, plate) if frames is not None and plate is not None else np.ascontiguousarray(canon[..., 3])
        placed[k] = (_premultiplied(canon), a8, per)

    def others_for(k: str):
        mine = placed[k][2]

        def at(f: int) -> list[Layer]:
            here = mine.get(f)
            if here is None:
                return []
            x0, y0, x1, y1 = here[1]
            out = []
            for j in order:
                q = placed[j][2].get(f) if j != k else None
                if q is None or q[2] <= 0:
                    continue
                a0, b0, a1, b1 = q[1]
                if a1 < x0 or a0 > x1 or b1 < y0 or b0 > y1:
                    continue
                prem, a8 = placed[j][0], placed[j][1]
                if q[3] < 1.0:
                    cut = int(round(prem.shape[1] * max(q[3], 0.0)))
                    prem, a8 = prem.copy(), a8.copy()
                    prem[:, cut:], a8[:, cut:] = 0, 0
                out.append(Layer(prem, q[0], q[2], rank[j] > rank[k], a8))
            return out

        return at

    return others_for


def matte_props(props: dict, frames: np.ndarray, plate, *, workers: int = 4, pad: int = PAD, skip=()) -> dict:
    """Textures v2 for every element in `props` (in place): `canon` becomes the matted padded texture, `texture_meta`
    records method, frames, confidence and padding, and a text reveal column is mapped onto the padded width. An
    element that fails keeps its binary texture (`texture_meta` method "binary", `texture_error` set). A still
    (unstable) mover is matted from its canonical frame only; a mover's filled-in pixels stay opaque. Keys in
    `skip` keep their texture (no `texture_meta`) but still hide or back the others."""
    t0 = time.perf_counter()
    keys = [k for k, p in props.items() if not k.startswith("_") and isinstance(p, dict) and "canon" in p and "raw" in p]
    if not keys:
        return {}
    others_for = _layers(props, keys, frames, as_plate(plate))

    def run(k):
        p = props[k]
        mover = bool(p.get("mover"))
        pk = 0 if p.get("kind") == "text" else pad   # R27: text keeps its box (HTML, AE and Lottie set glyphs in it)
        try:
            hidden = p.get("hidden")
            keep = (hidden, p["canon"][..., :3]) if mover and hidden is not None and np.any(hidden) else None
            rgba, meta = texture_v2(p["raw"], p["canon"][..., 3] > 127, frames, plate, others_at=others_for(k), pad=pk,
                                    kind_hint="mover" if mover else p.get("kind"),
                                    candidates=[int(p["cf"])] if mover and not p.get("stable") else None, keep=keep,
                                    ref_frame=int(p["cf"]) if p.get("cf") is not None else None)
            return k, rgba, meta, None
        except NotMatted as e:
            return k, None, None, ("note", str(e))
        except Exception as e:   # fail soft: today's binary texture, lower confidence, a message
            log.exception("textures v2 failed for %s", k)
            return k, None, None, ("error", f"{type(e).__name__}: {e}"[:160])

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        results = list(ex.map(run, [k for k in keys if k not in skip]))
    stats: dict = {}
    for k, rgba, meta, err in results:
        p = props[k]
        if err is not None:
            p["texture_meta"] = TextureMeta(method="binary", frames=[int(p.get("cf", 0))], confidence=METHOD_FACTOR["binary"],
                                            padding=0).model_dump()
            p["texture_error" if err[0] == "error" else "texture_note"] = err[1]
            stats["binary"] = stats.get("binary", 0) + 1
            continue
        pad_reveal(p["raw"], p["canon"].shape[1], meta.padding)
        p["canon"], p["texture_meta"] = rgba, meta.model_dump()
        stats[meta.method] = stats.get(meta.method, 0) + 1
    log.info("sprites textures v2 elements=%s workers=%s methods=%s %.2fs", len(keys), workers, stats, time.perf_counter() - t0)
    return stats
