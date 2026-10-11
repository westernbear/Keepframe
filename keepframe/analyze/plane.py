"""The text plane (D11, D13): a quad from robust cap/base lines of a glyph mask, its rectified patch, ECC homography
tracking from the settled frame outward, and the inverse of `projective_matrix` into render props, smoothed into an
n×11 raw array for `tracks_from_raw`. Coordinates are pixel indices, as the composite's warps use them."""
from __future__ import annotations
import math
import cv2
import numpy as np
from scipy.optimize import least_squares
from scipy.signal import savgol_filter
from scipy.stats import theilslopes
from ..ir.schema import PROPS
from ..ir.tracks import decompose_affine, projective_matrix
from .matting import as_plate

FIT = ("x", "y", "sx", "sy", "rot", "skx", "rx", "ry")
PAD = (0.1, 0.35)   # quad padding in cap heights: left/right, top/bottom
JUMP = 0.3          # a tracked corner moving more than JUMP·h in one frame is held


def text_lines(mask) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """Cap line and baseline of a glyph mask as y = a·x + b: Theil–Sen slopes of the column tops and bottoms (ink
    edges), the cap line at p10 of the tops (capitals and ascenders over x-height), the baseline at the median of
    the bottoms (descenders are outliers). None with fewer than 4 inked columns."""
    m = np.asarray(mask, bool)
    cols = np.flatnonzero(m.any(0))
    if len(cols) < 4:
        return None
    cols = cols[::math.ceil(len(cols) / 512)]   # Theil–Sen is quadratic in the columns
    sub = m[:, cols]
    top, bot = sub.argmax(0) - 0.5, len(m) - sub[::-1].argmax(0) - 0.5
    lines = []
    for y, q in ((top, 10), (bot, 50)):
        a = float(theilslopes(y, cols)[0])
        lines.append((a, float(np.percentile(y - a * cols, q))))
    return lines[0], lines[1]


def text_quad(mask, pad=PAD) -> tuple[np.ndarray, dict] | None:
    """The plane's quad TL, TR, BR, BL (scene px) and {"rot_deg", "converge_deg", "cap_px"}: the cap line and
    baseline cut by two sides through the ink's extremes along the baseline, padded pad[0]·h left/right and pad[1]·h
    top/bottom with h the cap height at that side. None when the lines are not measurable or cross the text.
    ponytail: sides are perpendicular to the baseline, exact for pure yaw at the stage's height; in-plane rot or a
    baseline off that height leans the true sides (corners 2–3 px off at rot 2–3° on a −30° yaw, the canon's stems
    leaning by as much); upgrade path: a vanishing point of the glyph stems."""
    lines = text_lines(mask)
    if lines is None:
        return None
    (ac, bc), (ab, bb) = lines
    u = np.array([1.0, ab]) / math.hypot(1.0, ab)
    n = np.array([-u[1], u[0]])                    # down the side, cap → base
    ys, xs = np.nonzero(mask)
    t = xs * u[0] + ys * u[1]

    def meet(a, b, t0):                            # the side p·u = t0 meets y = a·x + b
        x = (t0 - u[1] * b) / (u[0] + u[1] * a)
        return np.array([x, a * x + b])

    sides, hs = [], []
    for t0, s in ((t.min() - 0.5, -1.0), (t.max() + 0.5, 1.0)):
        h = float((meet(ab, bb, t0) - meet(ac, bc, t0)) @ n)
        if h < 1.0:
            return None
        cap, base = meet(ac, bc, t0 + s * pad[0] * h), meet(ab, bb, t0 + s * pad[0] * h)
        sides.append((cap + pad[1] * (cap - base), base + pad[1] * (base - cap)))
        hs.append(h)
    (tl, bl), (tr, br) = sides
    info = {"rot_deg": math.degrees(math.atan(ab)), "converge_deg": math.degrees(math.atan(ac) - math.atan(ab)),
            "cap_px": float(np.mean(hs))}
    return np.array([tl, tr, br, bl]), info


def quad_tilted(info, rot=2.0, converge=1.5) -> bool:
    """D12: the baseline turns ≥ `rot`° or the cap line and baseline converge ≥ `converge`°."""
    return abs(info["rot_deg"]) >= rot or abs(info["converge_deg"]) >= converge


def rectify(img, quad, wh=None) -> tuple[np.ndarray, np.ndarray]:
    """The quad's content as an upright (w, h) patch, by default its longest edges, and H canon→scene (patch px →
    scene px, the map the patch samples)."""
    q = np.asarray(quad, np.float32)
    if wh is None:
        e = np.linalg.norm(q - np.roll(q, -1, 0), axis=1)   # top, right, bottom, left
        wh = (max(1, int(round(float(max(e[0], e[2]))))), max(1, int(round(float(max(e[1], e[3]))))))
    w, h = wh
    H = cv2.getPerspectiveTransform(np.float32([[0, 0], [w, 0], [w, h], [0, h]]), q).astype(np.float64)
    return cv2.warpPerspective(img, H, (w, h), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP), H


def _corners(H, wh) -> np.ndarray:
    w, h = wh
    q = np.array([[0.0, 0, 1], [w, 0, 1], [w, h, 1], [0, h, 1]]) @ np.asarray(H, float).T
    return q[:, :2] / q[:, 2:]


def fit_props(H, wh, size, init=None) -> tuple[dict, float]:
    """Props (x, y, sx, sy, rot, skx, rx, ry) whose `projective_matrix` puts the corners of a canonical box `wh`
    (anchor at its centre) where H (canon→scene) does: 8-parameter least squares on the 4 corners, started from
    `init` (the previous frame) or from H's affine part at the anchor. Returns (props, largest corner residual px)."""
    w, h = wh
    Q = _corners(H, wh)
    L = np.array([[-w / 2, -h / 2, 1], [w / 2, -h / 2, 1], [w / 2, h / 2, 1], [-w / 2, h / 2, 1]])

    def residual(v):
        q = L @ projective_matrix(dict(zip(FIT, v)), size).T
        return (q[:, :2] / q[:, 2:] - Q).ravel()

    if init is None:
        A = np.asarray(H, float) @ np.array([[1.0, 0, w / 2], [0, 1, h / 2], [0, 0, 1]])
        A = A / A[2, 2]
        init = decompose_affine(np.c_[A[:2, :2] - np.outer(A[:2, 2], A[2, :2]), A[:2, 2]])   # d(scene)/d(local) at 0
    v = least_squares(residual, [float(init.get(k, 0.0)) for k in FIT], method="lm", x_scale="jac").x
    return dict(zip(FIT, map(float, v))), float(np.linalg.norm(residual(v).reshape(4, 2), axis=1).max())


def contrast(img, B) -> np.ndarray:
    """|I − B| summed over channels (float32): the image the plane is tracked on, for the template and every frame."""
    return np.abs(np.asarray(img, np.float32) - np.asarray(B, np.float32)).sum(-1)


def _ecc(img, B, template, H, margin, work, min_cc) -> np.ndarray | None:
    """H refined by ECC (MOTION_HOMOGRAPHY) of the template against the contrast crop around its predicted quad at
    ≤ `work` px; None when it fails or correlates below `min_cc`."""
    q = _corners(H, template.shape[1::-1])
    x0, y0 = np.maximum(np.floor(q.min(0) - margin).astype(int), 0)
    x1, y1 = np.minimum(np.ceil(q.max(0) + margin).astype(int), img.shape[1::-1])
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    crop = contrast(img[y0:y1, x0:x1], B[y0:y1, x0:x1] if np.ndim(B) == 3 else B)
    s = min(1.0, work / max(*crop.shape, *template.shape))
    tpl = template
    if s < 1.0:
        # ponytail: one ECC level at ≤ work px; upgrade path: coarse-to-fine ECC.
        crop = cv2.resize(crop, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        tpl = cv2.resize(template, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    S = np.diag([s, s, 1.0])
    Tc = np.array([[1.0, 0, -x0], [0, 1, -y0], [0, 0, 1]])
    W = S @ Tc @ H @ np.linalg.inv(S)                   # template px → crop px, both at the working scale
    crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 50, 1e-5)
    try:
        cc, W = cv2.findTransformECC(tpl, crop, (W / W[2, 2]).astype(np.float32), cv2.MOTION_HOMOGRAPHY, crit, None, 5)
    except cv2.error:
        return None
    if not cc >= min_cc:
        return None
    return np.linalg.inv(Tc) @ np.linalg.inv(S) @ W.astype(np.float64) @ S


def track_plane(frames, plate, template, H0, cf, span, *, work=384, min_cc=0.6) -> tuple[dict[int, np.ndarray], set[int]]:
    """H canon→scene for every frame of `span` (first, last), tracked from cf (where it is H0) outward: ECC of
    `template` (the rectified contrast at cf: `rectify(contrast(frames[cf], B), quad)`) against each frame's contrast,
    started from the neighbour's H. A frame whose ECC fails, correlates below `min_cc` or moves a corner more than
    0.3·h (h the cap height) keeps its neighbour's H and is returned in the held set.
    ponytail: a plain hold on failure; upgrade path: re-detection of the quad after a hold."""
    plate = as_plate(plate)
    template = np.asarray(template, np.float32)
    wh = template.shape[1::-1]
    q0 = _corners(H0, wh)
    h = (np.linalg.norm(q0[3] - q0[0]) + np.linalg.norm(q0[2] - q0[1])) / 2 / (1 + 2 * PAD[1])
    Hs, held = {cf: np.asarray(H0, np.float64)}, set()
    for step, stop in ((1, span[1] + 1), (-1, span[0] - 1)):
        H = Hs[cf]
        for f in range(cf + step, stop, step):
            got = _ecc(frames[f], plate.at(f), template, H, 2 * JUMP * h, work, min_cc)
            if got is None or np.linalg.norm(_corners(got, wh) - _corners(H, wh), axis=1).max() > JUMP * h:
                held.add(f)
            else:
                H = got
            Hs[f] = H
    return Hs, held


def plane_raw(Hs, wh, size, n, opacity) -> np.ndarray:
    """n×11 raw (PROPS order) from per-frame canon→scene homographies: `fit_props` each, started from the previous
    fit, then savgol(7, 2) over the geometry of each contiguous run of ≥ 5 frames (shorter runs stay as fitted).
    `opacity` is a scalar or per frame; sky is 0, reveal NaN, and rows outside Hs are NaN."""
    raw = np.full((n, len(PROPS)), np.nan)
    op = np.broadcast_to(np.asarray(opacity, float), (n,))
    p = None
    for f in sorted(Hs):
        p, _ = fit_props(Hs[f], wh, size, p)
        raw[f] = [{**p, "sky": 0.0, "opacity": op[f], "reveal": np.nan}[k] for k in PROPS]
    geo = [PROPS.index(k) for k in FIT]
    rows = np.flatnonzero(~np.isnan(raw[:, 0]))
    for run in np.split(rows, np.flatnonzero(np.diff(rows) > 1) + 1):
        if len(run) >= 5:
            raw[np.ix_(run, geo)] = savgol_filter(raw[np.ix_(run, geo)], min(7, len(run) - 1 + len(run) % 2), 2, axis=0)
    return raw
