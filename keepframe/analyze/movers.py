"""Movers: large elements that animate in place (the rotating globe behind a title). Pass 1 bakes them into the
plate or shreds them into fragments; here they are found from per-pixel temporal instability, claim the object
and shape tracks that are their fragments, get a per-frame mask and their own sprite layer at z 0, and leave the
plate. A stable mover gets a median-crop texture; an unstable one stays a still sprite (with a message) until
video sprites exist."""
from __future__ import annotations
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import cv2, numpy as np
from ..ir.colour import delta_e, srgb8_to_lab
from .plate import MIN_SEEN, OCR_PAD, SENTINEL, _fill_enclosed, _kernel, _workers, fill_holes, masked_median, sample_frames
from .sprites import RAW_COLS
from .text import reveal_exclusion_boxes

SCALE = 0.25              # work size of the instability map
UNSTABLE_DE = 6.0         # ΔE76 vs the median that makes a sample unstable
MASK_DE = 8.0             # ΔE76 vs the plate behind it that puts a pixel in a mover's mask …
KNOWN_DE = 3.0            # … or this, where that plate is a polynomial refill (ring RMS ≤ 2: known that well)
CLOSE_PX = 5              # closing radius (work px)
RING_PX = 8               # width of the ring that must stay stable (work px)
RING_U = 0.1              # median instability of that ring
FRAME_UNSTABLE = 0.30     # share of a component unstable at once, in ≥ min_unstable of the samples
CLAIM_INSIDE = 0.80       # a track is a fragment when this share of its pixels lies in the dilated component …
CLAIM_FRAMES = 0.60       # … in this share of its frames (pixels matching the plate behind, pass 1 halos, aside)
REFILL_PX = 16            # the component dilated by this (full px) is refilled from its ring: the plate behind it
MASK_DILATE = 2
STABLE_DE = 3.0           # median per-frame crop ΔE76 vs the temporal-median crop of a stable mover
EST_SHARE = 0.25          # an object track lasting this share of the shot …
EST_AREA = 0.25           # … whose area stays within ±25 % of its median …
EST_RIGID = 0.80          # … in this share of its frames is established: a layer of its own, not a fragment


@dataclass
class Mover:
    id: int
    frames: dict[int, tuple[tuple[int, int, int, int], np.ndarray]]   # per frame: mask cropped to its bbox
    claimed: list[int]                                                # object track ids
    residual: float
    stable: bool
    claimed_shapes: list[int] = field(default_factory=list)          # shape (junk OCR) track ids


def _established(t, n: int) -> bool:
    if len(t.regions) < max(MIN_SEEN, math.ceil(EST_SHARE * n)):
        return False
    areas = np.array([r.area for r in t.regions.values()], np.float64)
    return float(np.mean(np.abs(areas / np.median(areas) - 1) <= EST_AREA)) >= EST_RIGID


def _box(m, box, pad=0, value=True):
    x0, y0, x1, y1 = box
    m[max(0, y0 - pad):max(0, y1 + pad), max(0, x0 - pad):max(0, x1 + pad)] = value


def _paste(m, origin, bbox, mask, value=True):
    """Set m (whose top-left sits at `origin` in the frame) where a bbox-cropped mask is on; clipped."""
    ox, oy = origin
    x0, y0, x1, y1 = bbox
    h, w = m.shape
    a0, b0, a1, b1 = max(0, x0 - ox), max(0, y0 - oy), min(w, x1 - ox), min(h, y1 - oy)
    if a1 > a0 and b1 > b0:
        sub = mask[b0 + oy - y0:b1 + oy - y0, a0 + ox - x0:a1 + ox - x0].astype(bool)
        m[b0:b1, a0:a1][sub] = value


def established_mask(shape, text_tracks, obj_tracks, n, sample=None) -> np.ndarray:
    """Per sample (default: every frame), the pixels an established element covers: text boxes (+2 px), text
    reveal boxes, and the regions of object tracks that last (≥ 25 % of the n frames) and keep their area (±25 %
    in 80 % of their frames). Fragments of something animating in place do neither, so they stay visible."""
    sample = list(range(n)) if sample is None else list(sample)
    H, W = shape
    out = np.zeros((len(sample), H, W), bool)
    reveal = reveal_exclusion_boxes(list(text_tracks))
    keep = [t for t in obj_tracks if _established(t, n)]
    for j, f in enumerate(sample):
        for t in text_tracks:
            if f in t.boxes:
                _box(out[j], t.boxes[f].bbox, OCR_PAD)
        for b in reveal.get(f, ()):
            _box(out[j], b)
        for t in keep:
            if f in t.regions:
                _paste(out[j], (0, 0), t.regions[f].bbox, t.regions[f].mask)
    return out


def _work_size(W, H, scale):
    return max(1, round(W * scale)), max(1, round(H * scale))


def _deviation(frames, sample, established, size) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per sample, the work-size pixels with ΔE76 > UNSTABLE_DE against the median of the samples no established
    element covers (any coverage hides a work pixel); per pixel the number of such samples; the hidden pixels."""
    S, (w, h) = len(sample), size
    small = np.empty((S, h, w, 3), np.uint8)
    hidden = np.zeros((S, h, w), bool)

    def run(j):
        small[j] = cv2.resize(np.ascontiguousarray(frames[sample[j]]), size, interpolation=cv2.INTER_AREA)
        if established is not None:
            cover = np.ascontiguousarray(established[j]).view(np.uint8) * np.uint8(255)
            hidden[j] = cv2.resize(cover, size, interpolation=cv2.INTER_AREA) > 0

    with ThreadPoolExecutor(_workers()) as ex:
        list(ex.map(run, range(S)))
    med, seen = masked_median(small, range(S), hidden)
    ref = srgb8_to_lab(med)
    dev = np.empty((S, h, w), bool)
    for j in range(S):
        dev[j] = (delta_e(srgb8_to_lab(small[j]), ref) > UNSTABLE_DE) & ~hidden[j]
    return dev, seen, hidden


def instability(frames, sample, established, scale=SCALE) -> np.ndarray:
    """u: per work-size pixel, the share of its uncovered samples that are unstable (ΔE76 > 6 vs their median);
    NaN where fewer than MIN_SEEN samples show it. The only per-pixel instability map (ruling R3)."""
    H, W = frames.shape[1:3]
    dev, seen, _ = _deviation(frames, sample, established, _work_size(W, H, scale))
    u = dev.sum(0, dtype=np.float32) / np.maximum(seen, 1)
    u[seen < MIN_SEEN] = np.nan
    return u


def _unstable_share(frames, sample, established, comp, full_box, size) -> float:
    """Share of the samples in which ≥ FRAME_UNSTABLE of the component's visible pixels are unstable at once."""
    X0, Y0, X1, Y1 = full_box
    est = None if established is None else established[:, Y0:Y1, X0:X1]
    dev, _, hidden = _deviation(frames[:, Y0:Y1, X0:X1], sample, est, size)
    frac = dev[:, comp].sum(1) / np.maximum((~hidden[:, comp]).sum(1), 1)
    return float(np.mean(frac >= FRAME_UNSTABLE))


def _components(frames, sample, u, established, min_area, min_unstable, max_area) -> list[np.ndarray]:
    """Full-size (uint8) masks of the unstable components that qualify, in raster order."""
    H, W = frames.shape[1:3]
    h, w = u.shape
    sx, sy = W / w, H / h
    core = (np.nan_to_num(u, nan=0.0) >= min_unstable).astype(np.uint8)
    if not core.any():
        return []
    closed = cv2.morphologyEx(core, cv2.MORPH_CLOSE, _kernel(CLOSE_PX))
    whole = closed.copy()
    _fill_enclosed(whole, np.inf)   # stable insides (a title in front, an ocean) belong to the mover
    count, lab, stats, _ = cv2.connectedComponentsWithStats(whole, connectivity=8)
    out = []
    for k in range(1, count):
        x, y, ww, hh, area = (int(v) for v in stats[k])
        if area * sx * sy < min_area * H * W or ww * hh * sx * sy > max_area * H * W:
            continue
        comp = lab == k
        ring = cv2.dilate(comp.astype(np.uint8), _kernel(RING_PX)).astype(bool) & (whole == 0)
        ru = u[ring]
        ru = ru[np.isfinite(ru)]
        if ru.size == 0 or float(np.median(ru)) >= RING_U:   # its surroundings move too: plate animation
            continue
        box = (math.floor(x * sx), math.floor(y * sy), min(W, math.ceil((x + ww) * sx)), min(H, math.ceil((y + hh) * sy)))
        part = (comp & (closed > 0))[y:y + hh, x:x + ww]
        if _unstable_share(frames, sample, established, part, box, (ww, hh)) < min_unstable:
            continue
        out.append(cv2.resize(comp.astype(np.uint8), (W, H), interpolation=cv2.INTER_NEAREST))
    return out


def _inside(region, frames, behind_lab, f, bbox, mask=None) -> float:
    """Share of a track's pixels at frame f (region mask, or the whole OCR box) inside the dilated component,
    counting only pixels that differ from the plate behind (ΔE76 > 8): a pass 1 halo around a baked mover is
    plate, and a track that is all halo counts as inside."""
    H, W = region.shape
    x0, y0, x1, y1 = bbox
    a0, b0, a1, b1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
    if a1 <= a0 or b1 <= b0:
        return 0.0
    real = delta_e(srgb8_to_lab(frames[f, b0:b1, a0:a1]), behind_lab[b0:b1, a0:a1]) > MASK_DE
    if mask is not None:
        real &= mask[b0 - y0:b1 - y0, a0 - x0:a1 - x0].astype(bool)
    return float(region[b0:b1, a0:a1][real].mean()) if real.any() else 1.0


def _claims(region, tracks, taken, items, share) -> list:
    out = []
    for t in tracks:
        if t.id in taken:
            continue
        hits = [share(f, it) >= CLAIM_INSIDE for f, it in items(t)]
        if hits and sum(hits) >= CLAIM_FRAMES * len(hits):
            out.append(t)
            taken.add(t.id)
    return out


def _offsets(m: Mover) -> tuple[int, dict[int, np.ndarray]]:
    """The canonical frame (largest mask) and each frame's mask-centroid translation from it."""
    fs = sorted(m.frames)
    area = {f: int(m.frames[f][1].sum()) for f in fs}
    cf = max(fs, key=lambda f: area[f])
    c = {}
    for f in fs:
        (x0, y0, _, _), mk = m.frames[f]
        ys, xs = np.nonzero(mk)
        c[f] = np.array([xs.mean() + x0, ys.mean() + y0])
    return cf, {f: c[f] - c[cf] for f in fs}


def _aligned(m: Mover, frames, fs, d) -> tuple[tuple[int, int, int, int], np.ndarray, np.ndarray]:
    """Crops and masks of frames fs shifted back by their translation, in the canonical frame's coordinates."""
    H, W = frames.shape[1:3]
    sh = {f: np.rint(d[f]).astype(int) for f in fs}
    boxes = np.array([np.subtract(m.frames[f][0], [*sh[f], *sh[f]]) for f in fs])
    X0, Y0, X1, Y1 = boxes[:, 0].min(), boxes[:, 1].min(), boxes[:, 2].max(), boxes[:, 3].max()
    h, w = Y1 - Y0, X1 - X0
    crops = np.zeros((len(fs), h, w, 3), np.uint8)
    masks = np.zeros((len(fs), h, w), bool)
    for j, f in enumerate(fs):
        fx0, fy0 = X0 + sh[f][0], Y0 + sh[f][1]
        a0, b0, a1, b1 = max(0, fx0), max(0, fy0), min(W, fx0 + w), min(H, fy0 + h)
        crops[j, b0 - fy0:b1 - fy0, a0 - fx0:a1 - fx0] = frames[f, b0:b1, a0:a1]
        (x0, y0, x1, y1), mk = m.frames[f]
        masks[j, y0 - fy0:y1 - fy0, x0 - fx0:x1 - fx0] = mk
    return (int(X0), int(Y0), int(X1), int(Y1)), crops, masks


def _appearance(m: Mover, frames, fs, d):
    """(box, temporal-median crop, its mask, per-frame mean ΔE76 of each crop vs that median)."""
    box, crops, masks = _aligned(m, frames, fs, d)
    med, _ = masked_median(crops, range(len(fs)), ~masks)
    keep = masks.mean(0) >= 0.5
    ref = srgb8_to_lab(med)
    res = [float(delta_e(srgb8_to_lab(crops[j][sel]), ref[sel]).mean()) for j in range(len(fs))
           if (sel := masks[j] & keep).any()]
    return box, med, keep, res


def find_movers(frames, sample, u, obj_tracks, shape_tracks, *, plate, text_tracks=(), established=None,
                min_area=0.04, min_unstable=0.40, max_area=0.60) -> list[Mover]:
    """Components of u ≥ min_unstable (closed 5 px, insides filled) with area ≥ min_area, bbox ≤ max_area, a stable
    8 px ring and ≥ 30 % unstable in ≥ min_unstable of the samples. The plate behind is `plate` with the component
    dilated by REFILL_PX refilled from its ring. Each mover claims the object/shape tracks ≥ 80 % inside that
    dilated component in ≥ 60 % of their frames (never text). Per frame: dilate((ΔE(I_f, plate behind) > 8) ∩ bbox
    ∪ claimed, 2) minus text boxes and other tracks' regions; where the refill is a polynomial fit the threshold is
    KNOWN_DE, and claimed pixels that match the plate and reach the outside (pass 1 halos) are left out."""
    n, H, W = frames.shape[:3]
    cores = _components(frames, sample, u, established, min_area, min_unstable, max_area)
    if not cores:
        return []
    behind, none = plate, np.full((H, W, 3), SENTINEL, np.uint16)
    taken_o, taken_s = set(), set()
    cands = []
    for core in cores:
        region = cv2.dilate(core, _kernel(REFILL_PX)) > 0
        behind, _, info = fill_holes(behind, region, none)
        ys, xs = np.nonzero(region)
        x0, y0, x1, y1 = box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
        known = region[y0:y1, x0:x1] & all(h["method"] == "poly" for h in info)
        lab = srgb8_to_lab(behind)
        objs = _claims(region, obj_tracks, taken_o, lambda t: t.regions.items(),
                       lambda f, r: _inside(region, frames, lab, f, r.bbox, r.mask))
        shapes = _claims(region, shape_tracks, taken_s, lambda t: t.boxes.items(),
                         lambda f, b: _inside(region, frames, lab, f, b.bbox))
        cands.append((box, srgb8_to_lab(behind[y0:y1, x0:x1]), np.where(known, KNOWN_DE, MASK_DE).astype(np.float32), objs, shapes))
    others_o = [t for t in obj_tracks if t.id not in taken_o]
    others_s = [t for t in shape_tracks if t.id not in taken_s]

    def masks_at(f):
        out = []
        for (x0, y0, x1, y1), ref, thr, objs, shapes in cands:
            hit = delta_e(srgb8_to_lab(frames[f, y0:y1, x0:x1]), ref) > thr
            claimed = np.zeros_like(hit)
            for t in objs:
                if f in t.regions:
                    _paste(claimed, (x0, y0), t.regions[f].bbox, t.regions[f].mask)
            for t in shapes:
                if f in t.boxes:
                    _box(claimed, np.subtract(t.boxes[f].bbox, [x0, y0, x0, y0]))
            if claimed.any():   # keep the claimed pixels the mover encloses, drop the halo that reaches the outside
                _, labels = cv2.connectedComponents(np.pad((~hit).astype(np.uint8), 1, constant_values=1), connectivity=4)
                claimed &= labels[1:-1, 1:-1] != labels[0, 0]
            m = cv2.dilate((hit | claimed).astype(np.uint8), _kernel(MASK_DILATE)) > 0
            for t in text_tracks:   # text and other layers are drawn above the mover
                if f in t.boxes:
                    _box(m, np.subtract(t.boxes[f].bbox, [x0, y0, x0, y0]), OCR_PAD, False)
            for t in others_s:
                if f in t.boxes:
                    _box(m, np.subtract(t.boxes[f].bbox, [x0, y0, x0, y0]), 0, False)
            for t in others_o:
                if f in t.regions:
                    _paste(m, (x0, y0), t.regions[f].bbox, t.regions[f].mask, False)
            if not m.any():
                out.append(None)
                continue
            ys, xs = np.nonzero(m)
            a0, b0, a1, b1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
            out.append(((x0 + a0, y0 + b0, x0 + a1, y0 + b1), m[b0:b1, a0:a1].copy()))
        return out

    with ThreadPoolExecutor(_workers()) as ex:
        per_frame = list(ex.map(masks_at, range(n)))
    movers = []
    for i, (_, _, _, objs, shapes) in enumerate(cands):
        fr = {f: per_frame[f][i] for f in range(n) if per_frame[f][i] is not None}
        if not fr:
            continue
        m = Mover(id=len(movers) + 1, frames=fr, residual=0.0, stable=False,
                  claimed=sorted(t.id for t in objs), claimed_shapes=sorted(t.id for t in shapes))
        fs = sorted(fr)
        _, d = _offsets(m)
        res = _appearance(m, frames, [fs[k] for k in sample_frames(len(fs))], d)[3]
        m.residual = float(np.median(res)) if res else math.inf
        m.stable = m.residual < STABLE_DE
        movers.append(m)
    return movers


def mover_cover(movers, f, shape) -> np.ndarray:
    """Full-size mask of every mover at frame f."""
    out = np.zeros(shape, bool)
    for m in movers:
        if f in m.frames:
            _paste(out, (0, 0), *m.frames[f])
    return out


def _peel(rgb, mask, plate, box) -> np.ndarray:
    """Mask pixels that match the plate behind them and connect to the outside are background (the 2 px dilation,
    a claimed halo): they get α 0, so a recoloured background shows no ring."""
    x0, y0, x1, y1 = box
    H, W = plate.shape[:2]
    bg = np.zeros(mask.shape, bool)
    a0, b0, a1, b1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
    if a1 > a0 and b1 > b0:
        sub = (slice(b0 - y0, b1 - y0), slice(a0 - x0, a1 - x0))
        bg[sub] = delta_e(srgb8_to_lab(rgb[sub]), srgb8_to_lab(plate[b0:b1, a0:a1])) <= MASK_DE
    free = np.pad((~mask | bg).astype(np.uint8), 1, constant_values=1)
    _, lab = cv2.connectedComponents(free, connectivity=4)
    return mask & (lab[1:-1, 1:-1] != lab[0, 0])


def mover_props(m: Mover, frames, plate, n_frames) -> dict:
    """Sprite props at z 0, translated by its mask centroid. Stable: the temporal-median crop (Task 6 mattes
    it); unstable: the canonical frame's crop, a still until video sprites exist."""
    fs = sorted(m.frames)
    cf, d = _offsets(m)
    if m.stable:
        box, rgb, mask, _ = _appearance(m, frames, [fs[k] for k in sample_frames(len(fs))], d)
    else:
        box, mask = m.frames[cf]
        rgb = frames[cf, box[1]:box[3], box[0]:box[2]]
    alpha = _peel(rgb, mask, plate, box)
    canon = np.dstack([rgb, alpha.astype(np.uint8) * 255])
    raw = np.full((n_frames, len(RAW_COLS)), np.nan)
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    for f in fs:
        raw[f] = [cx + d[f][0], cy + d[f][1], 1.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    return {"raw": raw, "canon": canon, "cf": cf, "kind": "sprite", "z": 0, "first": fs[0], "last": fs[-1],
            "mover": True, "stable": m.stable, "residual": round(m.residual, 3)}
