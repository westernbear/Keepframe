# refstudio/analyze/sprites.py
from __future__ import annotations
import math
from itertools import combinations
import cv2, numpy as np
from ..ir.tracks import affine_matrix, decompose_affine
from .regions import Region
from .tracking import ObjectTrack

RAW_COLS = ("x", "y", "sx", "sy", "rot", "skx", "sky", "opacity")


def canonical_texture(track: ObjectTrack, frames: np.ndarray) -> tuple[np.ndarray, int]:
    cf = max(track.regions, key=lambda f: track.regions[f].area)
    r = track.regions[cf]
    x0, y0, x1, y1 = r.bbox
    crop = frames[cf][y0:y1, x0:x1]
    rgba = np.dstack([crop, (r.mask * 255).astype(np.uint8)])
    return rgba, cf


def _orientation(mask: np.ndarray) -> tuple[float, float]:
    m = cv2.moments(mask.astype(np.uint8), binaryImage=True)
    if m["m00"] == 0:
        return 0.0, 1.0
    mu20, mu02, mu11 = m["mu20"] / m["m00"], m["mu02"] / m["m00"], m["mu11"] / m["m00"]
    theta = 0.5 * math.atan2(2 * mu11, mu20 - mu02)
    l1 = 0.5 * (mu20 + mu02) + 0.5 * math.sqrt(4 * mu11 ** 2 + (mu20 - mu02) ** 2)
    l2 = 0.5 * (mu20 + mu02) - 0.5 * math.sqrt(4 * mu11 ** 2 + (mu20 - mu02) ** 2)
    return math.degrees(theta), math.sqrt(max(l1, 1e-9) / max(l2, 1e-9))


def _mask_max_side(region: Region) -> float:
    ys, xs = np.nonzero(region.mask)
    if len(xs) == 0:
        return 1.0
    pts = np.column_stack([xs, ys]).astype(np.float32)
    (_, _), (w, h), _ = cv2.minAreaRect(pts)
    return float(max(w, h))


def props_from_moments(region: Region, canon: np.ndarray) -> dict[str, float]:
    canon_mask = canon[..., 3] > 127
    s = math.sqrt(region.area / max(int(canon_mask.sum()), 1))
    th_c, aspect = _orientation(canon_mask)
    th_r, _ = _orientation(region.mask)
    rot = (th_r - th_c) if aspect >= 1.25 else 0.0
    rot = (rot + 90) % 180 - 90
    return {"x": region.centroid[0], "y": region.centroid[1], "sx": s, "sy": s, "rot": rot, "skx": 0.0, "sky": 0.0, "opacity": 1.0}


def _region_scale(region: Region, r_lo: Region, r_cf: Region, sx_cf: float, canon: np.ndarray) -> float:
    """Scale vs track-first; area-based when upright, min-area-rect when rotated."""
    canon_mask = canon[..., 3] > 127
    th_r, ar = _orientation(region.mask)
    th_c, ac = _orientation(canon_mask)
    if ac >= 1.25 or ar >= 1.25 or abs(th_r - th_c) > 5:
        side = _mask_max_side(region) / max(_mask_max_side(r_lo), 1e-6)
        side_cf = _mask_max_side(r_cf) / max(_mask_max_side(r_lo), 1e-6)
        cal = side_cf / max(sx_cf, 1e-6)
        if ac < 1.25 and ar < 1.25:
            cal *= 1.04
        return side * cal
    return sx_cf * math.sqrt(region.area / max(r_cf.area, 1))


def _canon_anchor_px(canon: np.ndarray, anchor: tuple[float, float]) -> tuple[float, float]:
    h, w = canon.shape[:2]
    return anchor[0] * w, anchor[1] * h


def refine_ecc(frame: np.ndarray, region: Region, canon: np.ndarray, anchor: tuple[float, float],
               init: dict[str, float], margin: int = 8) -> dict[str, float]:
    """ECC on a crop around the region so other objects do not pull the warp."""
    H, W = frame.shape[:2]
    x0, y0 = max(0, region.bbox[0] - margin), max(0, region.bbox[1] - margin)
    x1, y1 = min(W, region.bbox[2] + margin), min(H, region.bbox[3] + margin)
    ax, ay = _canon_anchor_px(canon, anchor)
    L = np.array([[1.0, 0.0, -ax], [0.0, 1.0, -ay], [0.0, 0.0, 1.0]])
    Tc = np.array([[1.0, 0.0, -x0], [0.0, 1.0, -y0], [0.0, 0.0, 1.0]])
    M_tex_to_crop = Tc @ affine_matrix(init) @ L
    tpl = cv2.cvtColor(canon[..., :3], cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0
    tpl *= canon[..., 3].astype(np.float32) / 255.0
    img = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0
    try:
        crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 60, 1e-5)
        init_crop_to_tex = np.linalg.inv(M_tex_to_crop)[:2].astype(np.float32)
        _, w_crop_to_tex = cv2.findTransformECC(img, tpl, init_crop_to_tex, cv2.MOTION_AFFINE, crit, None, 5)
        M2 = np.linalg.inv(Tc) @ np.linalg.inv(np.vstack([w_crop_to_tex, [0, 0, 1]]))
    except cv2.error:
        return init
    props = decompose_affine(M2 @ np.linalg.inv(L))
    if math.hypot(props["x"] - init["x"], props["y"] - init["y"]) > 8 or not (0.2 < props["sx"] < 5) or not (0.2 < props["sy"] < 5):
        return init
    props["opacity"] = init["opacity"]
    return props


def _refine_ecc_xy(frame: np.ndarray, region: Region, canon: np.ndarray, init: dict[str, float]) -> dict[str, float]:
    """Refine position/rotation; keep moment scale (ECC often shrinks sx on rotated objects)."""
    sx, sy = init["sx"], init["sy"]
    props = refine_ecc(frame, region, canon, (0.5, 0.5), init)
    props["sx"], props["sy"] = sx, sy
    return props


def estimate_opacity(frame: np.ndarray, region: Region, canon_color: tuple, bg_rgb: tuple) -> float:
    x0, y0, x1, y1 = region.bbox
    px = frame[y0:y1, x0:x1][region.mask].astype(np.float32)
    c = np.array(canon_color, np.float32) - np.array(bg_rgb, np.float32)
    n = float(np.dot(c, c))
    if n < 1e-6 or len(px) == 0:
        return 1.0
    a = ((px - np.array(bg_rgb, np.float32)) @ c) / n
    return float(np.clip(np.median(a), 0.0, 1.0))


def z_order(tracks: list[ObjectTrack], frames: np.ndarray, bg_rgb: tuple) -> dict[int, int]:
    # ponytail: one z per object from overlap votes; per-frame z when votes flip is the upgrade path.
    above: dict[tuple[int, int], int] = {}
    for a, b in combinations(tracks, 2):
        for f in set(a.regions) & set(b.regions):
            ra, rb = a.regions[f], b.regions[f]
            x0, y0 = max(ra.bbox[0], rb.bbox[0]), max(ra.bbox[1], rb.bbox[1])
            x1, y1 = min(ra.bbox[2], rb.bbox[2]), min(ra.bbox[3], rb.bbox[3])
            if x1 <= x0 or y1 <= y0:
                continue
            ma = ra.mask[y0 - ra.bbox[1]:y1 - ra.bbox[1], x0 - ra.bbox[0]:x1 - ra.bbox[0]]
            mb = rb.mask[y0 - rb.bbox[1]:y1 - rb.bbox[1], x0 - rb.bbox[0]:x1 - rb.bbox[0]]
            va, vb = int(ma.sum()), int(mb.sum())
            if va != vb:
                key = (a.id, b.id) if va > vb else (b.id, a.id)
                above[key] = above.get(key, 0) + 1
    ids = sorted((t.id for t in tracks), key=lambda i: next(t.first for t in tracks if t.id == i))
    score = {i: 0 for i in ids}
    for (top, bottom), v in above.items():
        score[top] += v; score[bottom] -= v
    order = sorted(ids, key=lambda i: (score[i], ids.index(i)))
    return {i: k + 1 for k, i in enumerate(order)}


def sprite_props(track: ObjectTrack, frames: np.ndarray, bg_rgb: tuple, n_frames: int, first_frame: int,
                 use_ecc: bool = True) -> tuple[np.ndarray, np.ndarray, int]:
    canon, cf = canonical_texture(track, frames)
    r_cf, r_lo = track.regions[cf], track.regions[track.first]
    sx_cf = math.sqrt(r_cf.area / max(r_lo.area, 1))
    med_area = float(np.median([r.area for r in track.regions.values()]))
    canon_color = track.regions[track.last].color
    raw = np.full((n_frames, len(RAW_COLS)), np.nan)
    for f, r in track.regions.items():
        if r.area < 0.85 * med_area:
            continue
        p = props_from_moments(r, canon)
        p["sx"] = p["sy"] = _region_scale(r, r_lo, r_cf, sx_cf, canon)
        if use_ecc:
            p = _refine_ecc_xy(frames[f], r, canon, p)
        p["opacity"] = estimate_opacity(frames[f], r, canon_color, bg_rgb)
        raw[f - first_frame] = [p[c] for c in RAW_COLS]
    return raw, canon, cf
