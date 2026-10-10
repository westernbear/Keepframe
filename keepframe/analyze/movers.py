"""Movers: large elements that animate in place (the rotating globe behind a title). Pass 1 bakes them into the
plate or shreds them into fragments; here they are found from per-pixel temporal instability, claim the object
and shape tracks that are their fragments, get a per-frame mask and their own sprite layer at z −1 (below every
other layer, above the plate), and leave the plate. A stable mover gets a median-crop texture; an unstable one becomes
a video sprite (Task 13: its per-frame masked crops as RGBA WebM, the still texture its poster), or stays a still
sprite with a message when the video cannot be made. Mask thresholds follow the measured frame noise."""
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
KNOWN_DE = 3.0            # … or this, where that plate is a polynomial refill (ring RMS ≤ 2: known that well) …
NOISE_K = 3.0             # … but never below this many times the median frame-vs-plate ΔE76 just outside it
NOISE_PX = 8              # width of that ring (full px)
SPECK_SHARE = 0.002       # mask pieces smaller than this share of the component (≥ SPECK_MIN px) are noise
SPECK_MIN = 4
PRESENT = 0.10            # a frame holds the mover when its mask covers this share of the component
Z = -1                    # movers draw below every other layer and above the plate
CROSS = cv2.getStructuringElement(cv2.MORPH_CROSS, (3, 3))
CLOSE_PX = 2              # closing radius (work px); at 5 a sprite's trail 20 px off joined the mover (eval seed 8)
# ponytail: a trail within ~2 × CLOSE_PX work px still joins the core and its track is claimed; upgrade: take the
# pixels one moving, unclaimed track explains out of u before closing.
RING_PX = 8               # width of the ring that must stay stable (work px)
RING_U = 0.39             # median instability of that ring; at 0.1 ig2's globe failed: its full-frame opening leaves
#                           every pixel unstable in 18 of 48 samples (ring median 0.375)
# ponytail: ring pixels are below min_unstable (0.40), so this test now only stops rings almost that unstable; plate
# animation that leaves the ring unstable < 39 % of the time is left to max_area and the unstable share. Upgrade:
# judge the ring per sample (count only the samples in which the ring is stable).
FRAME_UNSTABLE = 0.30     # share of a component unstable at once, in ≥ min_unstable of the samples
CLAIM_INSIDE = 0.80       # a track is a fragment when this share of its pixels lies in the dilated component …
CLAIM_FRAMES = 0.60       # … in this share of its frames (pixels matching the plate behind, pass 1 halos, aside)
REFILL_PX = 16            # the component dilated by this (full px) is refilled from its ring: the plate behind it
HALO_PX = 32              # pass 1's low-pass cell: its halos around a baked mover reach this far (full px)
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
    threshold: float = MASK_DE                                        # ΔE76 vs the plate that made its masks
    cut: dict = field(default_factory=dict)   # per frame (bbox, mask): its pixels under text boxes, left out of frames


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


def _inside(region, near, frames, behind_lab, real_de, noisy, f, bbox, mask=None) -> float:
    """Share of a track's pixels at frame f (region mask, or the whole OCR box) inside the dilated component,
    counting only pixels that differ from the plate behind (ΔE76 > real_de: 8, or the noise floor above it; opened
    like the masks when noise sets it): a pass 1 halo around a baked mover is plate. When none does (a halo, or a
    faint layer): 0 unless the track overlaps the dilated component, else the share of all its pixels within
    HALO_PX of the component, where pass 1's halos lie."""
    H, W = region.shape
    x0, y0, x1, y1 = bbox
    a0, b0, a1, b1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
    if a1 <= a0 or b1 <= b0:
        return 0.0
    own = np.ones((b1 - b0, a1 - a0), bool) if mask is None else mask[b0 - y0:b1 - y0, a0 - x0:a1 - x0].astype(bool)
    real = delta_e(srgb8_to_lab(frames[f, b0:b1, a0:a1]), behind_lab[b0:b1, a0:a1]) > real_de
    if noisy:
        real = cv2.morphologyEx(real.astype(np.uint8), cv2.MORPH_OPEN, CROSS) > 0
    real &= own
    if real.any():
        return float(region[b0:b1, a0:a1][real].mean())
    if not (region[b0:b1, a0:a1] & own).any():   # a track must overlap the dilated component
        return 0.0
    return float(near[b0:b1, a0:a1][own].mean())


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


def _aligned(m: Mover, frames, fs, d):
    """Crops, masks and text cuts of frames fs shifted back by their translation, in the canonical frame's
    coordinates."""
    H, W = frames.shape[1:3]
    sh = {f: np.rint(d[f]).astype(int) for f in fs}
    parts = [(f, b) for f in fs for b in (m.frames[f], m.cut.get(f)) if b is not None]
    boxes = np.array([np.subtract(b[0], [*sh[f], *sh[f]]) for f, b in parts])
    X0, Y0, X1, Y1 = boxes[:, 0].min(), boxes[:, 1].min(), boxes[:, 2].max(), boxes[:, 3].max()
    h, w = Y1 - Y0, X1 - X0
    crops = np.zeros((len(fs), h, w, 3), np.uint8)
    masks = np.zeros((len(fs), h, w), bool)
    cuts = np.zeros((len(fs), h, w), bool)
    for j, f in enumerate(fs):
        fx0, fy0 = X0 + sh[f][0], Y0 + sh[f][1]
        a0, b0, a1, b1 = max(0, fx0), max(0, fy0), min(W, fx0 + w), min(H, fy0 + h)
        crops[j, b0 - fy0:b1 - fy0, a0 - fx0:a1 - fx0] = frames[f, b0:b1, a0:a1]
        for out, part in ((masks, m.frames[f]), (cuts, m.cut.get(f))):
            if part is not None:
                (x0, y0, x1, y1), mk = part
                out[j, y0 - fy0:y1 - fy0, x0 - fx0:x1 - fx0] = mk
    return (int(X0), int(Y0), int(X1), int(Y1)), crops, masks, cuts


def _appearance(m: Mover, frames, fs, d):
    """(box, temporal-median crop, its mask, pixels text hid in most frames, per-frame mean ΔE76 of each crop vs
    that median)."""
    box, crops, masks, cuts = _aligned(m, frames, fs, d)
    med, _ = masked_median(crops, range(len(fs)), ~masks)
    keep = masks.mean(0) >= 0.5
    hidden = ~keep & ((masks | cuts).mean(0) >= 0.5)
    ref = srgb8_to_lab(med)
    res = [float(delta_e(srgb8_to_lab(crops[j][sel]), ref[sel]).mean()) for j in range(len(fs))
           if (sel := masks[j] & keep).any()]
    return box, med, keep, hidden, res


def _noise(frames, sample, lab, region, established) -> float:
    """Median ΔE76 of the samples against the plate in a NOISE_PX ring just outside the dilated component (pixels an
    established element covers left out): the frame-to-plate noise a mask threshold must stay above."""
    ring = (cv2.dilate(region.astype(np.uint8), _kernel(NOISE_PX)) > 0) & ~region
    ys, xs = np.nonzero(ring)
    if not len(ys):
        return 0.0
    step = max(1, len(ys) // 4000)
    ys, xs = ys[::step], xs[::step]
    ref = lab[ys, xs]
    out = []
    for j in sample_frames(len(sample), 12):
        keep = ~established[j][ys, xs] if established is not None else slice(None)
        out.append(delta_e(srgb8_to_lab(frames[sample[j]][ys, xs][keep]), ref[keep]))
    vals = np.concatenate(out)
    return float(np.median(vals)) if vals.size else 0.0


def _despeck(m, min_px) -> np.ndarray:
    """m without its 8-connected pieces smaller than min_px (noise above the threshold)."""
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m.astype(np.uint8), connectivity=8)
    keep = stats[:, cv2.CC_STAT_AREA] >= min_px
    keep[0] = False
    return keep[lab]


def _tight(x0, y0, m):
    ys, xs = np.nonzero(m)
    a0, b0, a1, b1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
    return (x0 + a0, y0 + b0, x0 + a1, y0 + b1), m[b0:b1, a0:a1].copy()


def find_movers(frames, sample, u, obj_tracks, shape_tracks, *, plate, text_tracks=(), established=None,
                min_area=0.04, min_unstable=0.40, max_area=0.60) -> list[Mover]:
    """Components of u ≥ min_unstable (closed 2 px, insides filled) with area ≥ min_area, bbox ≤ max_area, a stable
    8 px ring and ≥ 30 % unstable in ≥ min_unstable of the samples. The plate behind is `plate` with the component
    dilated by REFILL_PX refilled from its ring. Each mover claims the object/shape tracks ≥ 80 % inside that
    dilated component in ≥ 60 % of their frames (never text). Per frame: dilate((ΔE(I_f, plate behind) > thr) ∩ bbox
    ∪ claimed, 2) minus text boxes and other tracks' regions, where thr is 8, or KNOWN_DE inside a polynomial refill,
    but at least NOISE_K × the measured noise; pieces smaller than a speck are dropped, claimed pixels that match the
    plate and reach the outside (pass 1 halos) are left out, and a frame whose mask covers < PRESENT of the
    component does not hold the mover."""
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
        lab = srgb8_to_lab(behind)
        floor = NOISE_K * _noise(frames, sample, lab, region, established)
        base = KNOWN_DE if all(h["method"] == "poly" for h in info) else MASK_DE
        inside = max(base, floor)
        thr = np.where(region[y0:y1, x0:x1], inside, max(MASK_DE, floor)).astype(np.float32)
        near = cv2.dilate(core, _kernel(HALO_PX)) > 0
        real_de, noisy = max(MASK_DE, floor), floor > base
        objs = _claims(region, obj_tracks, taken_o, lambda t: t.regions.items(),
                       lambda f, r: _inside(region, near, frames, lab, real_de, noisy, f, r.bbox, r.mask))
        shapes = _claims(region, shape_tracks, taken_s, lambda t: t.boxes.items(),
                         lambda f, b: _inside(region, near, frames, lab, real_de, noisy, f, b.bbox))
        area = int(np.count_nonzero(core))
        cands.append(dict(box=box, ref=lab[y0:y1, x0:x1].copy(), thr=thr, inside=inside, noisy=noisy, objs=objs,
                          shapes=shapes, core=core[y0:y1, x0:x1] > 0, area=area, speck=max(SPECK_MIN, SPECK_SHARE * area)))
    others_o = [t for t in obj_tracks if t.id not in taken_o]
    others_s = [t for t in shape_tracks if t.id not in taken_s]

    def masks_at(f):
        out = []
        for c in cands:
            x0, y0, x1, y1 = c["box"]
            shift = [x0, y0, x0, y0]
            hit = delta_e(srgb8_to_lab(frames[f, y0:y1, x0:x1]), c["ref"]) > c["thr"]
            if c["noisy"]:   # noise sets the threshold: a mask pixel needs mask neighbours (no 1 px specks or spurs)
                hit = cv2.morphologyEx(hit.astype(np.uint8), cv2.MORPH_OPEN, CROSS) > 0
            hit = _despeck(hit, c["speck"])
            claimed = np.zeros_like(hit)
            for t in c["objs"]:
                if f in t.regions:
                    _paste(claimed, (x0, y0), t.regions[f].bbox, t.regions[f].mask)
            for t in c["shapes"]:
                if f in t.boxes:
                    _box(claimed, np.subtract(t.boxes[f].bbox, shift))
            if claimed.any():   # keep the claimed pixels the mover encloses, drop the halo that reaches the outside
                _, labels = cv2.connectedComponents(np.pad((~hit).astype(np.uint8), 1, constant_values=1), connectivity=4)
                claimed &= labels[1:-1, 1:-1] != labels[0, 0]
            if np.count_nonzero((hit | claimed) & c["core"]) < PRESENT * c["area"]:
                out.append((None, None))   # noise, or the mover is not there yet
                continue
            m = cv2.dilate((hit | claimed).astype(np.uint8), _kernel(MASK_DILATE)) > 0
            text = np.zeros_like(m)
            for t in text_tracks:   # text and other layers are drawn above the mover
                if f in t.boxes:
                    _box(text, np.subtract(t.boxes[f].bbox, shift), OCR_PAD)
            cut = m & text & c["core"]
            m &= ~text
            for t in others_s:
                if f in t.boxes:
                    _box(m, np.subtract(t.boxes[f].bbox, shift), 0, False)
            for t in others_o:
                if f in t.regions:
                    _paste(m, (x0, y0), t.regions[f].bbox, t.regions[f].mask, False)
            out.append((_tight(x0, y0, m) if m.any() else None, _tight(x0, y0, cut) if cut.any() else None))
        return out

    with ThreadPoolExecutor(_workers()) as ex:
        per_frame = list(ex.map(masks_at, range(n)))
    movers = []
    for i, c in enumerate(cands):
        fr = {f: per_frame[f][i][0] for f in range(n) if per_frame[f][i][0] is not None}
        if not fr:
            continue
        m = Mover(id=len(movers) + 1, frames=fr, residual=0.0, stable=False, threshold=float(c["inside"]),
                  claimed=sorted(t.id for t in c["objs"]), claimed_shapes=sorted(t.id for t in c["shapes"]),
                  cut={f: per_frame[f][i][1] for f in fr if per_frame[f][i][1] is not None})
        fs = sorted(fr)
        _, d = _offsets(m)
        res = _appearance(m, frames, [fs[k] for k in sample_frames(len(fs))], d)[4]
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


def _peel(rgb, mask, plate, box, threshold) -> np.ndarray:
    """Mask pixels within the mask threshold of the plate behind them that connect to the outside are background
    (the 2 px dilation, a claimed halo): they get α 0, so a recoloured background shows no ring."""
    x0, y0, x1, y1 = box
    H, W = plate.shape[:2]
    bg = np.zeros(mask.shape, bool)
    a0, b0, a1, b1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
    if a1 > a0 and b1 > b0:
        sub = (slice(b0 - y0, b1 - y0), slice(a0 - x0, a1 - x0))
        bg[sub] = delta_e(srgb8_to_lab(rgb[sub]), srgb8_to_lab(plate[b0:b1, a0:a1])) <= threshold
    free = np.pad((~mask | bg).astype(np.uint8), 1, constant_values=1)
    _, lab = cv2.connectedComponents(free, connectivity=4)
    return mask & (lab[1:-1, 1:-1] != lab[0, 0])


def _canonical(m: Mover, frames, cf, d):
    """(box, rgb, mask, pixels to fill): stable, the aligned temporal-median crop; unstable, the canonical frame."""
    if m.stable:
        fs = sorted(m.frames)
        box, rgb, mask, hidden, _ = _appearance(m, frames, [fs[k] for k in sample_frames(len(fs))], d)
        return box, rgb, mask, hidden
    (bx0, by0, bx1, by1), mk = m.frames[cf]
    cut = m.cut.get(cf)
    x0, y0, x1, y1 = box = (bx0, by0, bx1, by1) if cut is None else (
        min(bx0, cut[0][0]), min(by0, cut[0][1]), max(bx1, cut[0][2]), max(by1, cut[0][3]))
    mask, hidden = np.zeros((y1 - y0, x1 - x0), bool), np.zeros((y1 - y0, x1 - x0), bool)
    _paste(mask, (x0, y0), (bx0, by0, bx1, by1), mk)
    if cut is not None:
        _paste(hidden, (x0, y0), *cut)
    return box, frames[cf, y0:y1, x0:x1].copy(), mask, hidden & ~mask


def mover_props(m: Mover, frames, plate, n_frames) -> dict:
    """Sprite props at z −1 (below every other layer), translated by its mask centroid. Stable: the
    temporal-median crop (Task 6 mattes it); unstable: the canonical frame's crop (the poster of its video sprite,
    whose frames are cut at whole pixels: its track moves by whole pixels too). Pixels text boxes hid are inpainted
    and opaque (`synthetic` counts them), so the gaps between glyphs show the mover, not the plate."""
    fs = sorted(m.frames)
    cf, d = _offsets(m)
    box, rgb, mask, hidden = _canonical(m, frames, cf, d)
    alpha = _peel(rgb, mask, plate, box, m.threshold) | hidden
    if hidden.any():
        rgb = cv2.inpaint(np.ascontiguousarray(rgb), hidden.astype(np.uint8), 5, cv2.INPAINT_TELEA)
    canon = np.dstack([rgb, alpha.astype(np.uint8) * 255])
    raw = np.full((n_frames, len(RAW_COLS)), np.nan)
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    for f in fs:
        dx, dy = d[f] if m.stable else np.rint(d[f])
        raw[f] = [cx + dx, cy + dy, 1.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    return {"raw": raw, "canon": canon, "cf": cf, "kind": "sprite", "z": Z, "first": fs[0], "last": fs[-1],
            "mover": True, "stable": m.stable, "residual": round(m.residual, 3), "synthetic": int(hidden.sum()),
            "hidden": hidden}   # filled-in (inpainted) pixels: textures v2 keeps them opaque


def mover_frames(m: Mover, frames, plate, pad: int = 0):
    """An unstable mover at each frame as f → RGBA uint8 in its texture's geometry grown by `pad` a side: frame f
    cut at the canonical box moved by that frame's whole-pixel centroid shift (as its track), opaque on the frame's
    mask (pixels within its threshold of the plate behind, `plate.at(f)`, that reach the outside peeled; pixels text
    hid filled in and opaque), clear where the mover is not there. What its video plays, and what matting puts
    under the layers above it."""
    cf, d = _offsets(m)
    box = _canonical(m, frames, cf, d)[0]
    th, tw = box[3] - box[1] + 2 * pad, box[2] - box[0] + 2 * pad
    H, W = frames.shape[1:3]

    def frame(f: int) -> np.ndarray:
        out = np.zeros((th, tw, 4), np.uint8)
        if f not in m.frames:
            return out
        dx, dy = (int(v) for v in np.rint(d[f]))
        X0, Y0 = box[0] - pad + dx, box[1] - pad + dy
        a0, b0, a1, b1 = max(0, X0), max(0, Y0), min(W, X0 + tw), min(H, Y0 + th)
        if a1 <= a0 or b1 <= b0:
            return out
        rgb = np.zeros((th, tw, 3), np.uint8)
        rgb[b0 - Y0:b1 - Y0, a0 - X0:a1 - X0] = frames[f, b0:b1, a0:a1]
        mask = np.zeros((th, tw), bool)
        _paste(mask, (X0, Y0), *m.frames[f])
        alpha = _peel(rgb, mask, plate.at(f), (X0, Y0, X0 + tw, Y0 + th), m.threshold)
        if f in m.cut:
            hid = np.zeros((th, tw), bool)
            _paste(hid, (X0, Y0), *m.cut[f])
            if hid.any():
                rgb = cv2.inpaint(rgb, hid.astype(np.uint8), 5, cv2.INPAINT_TELEA)
                alpha |= hid
        out[..., :3] = rgb
        out[..., 3] = alpha.astype(np.uint8) * 255
        return out

    return frame


def mover_video(m: Mover, p: dict, frames, plate, fps: float, out) -> tuple[str | None, str | None]:
    """An unstable mover as an RGBA WebM (mover_frames in its matted, padded texture's geometry), frame i being
    frame first + i. Returns (path, None) or (None, code)."""
    from . import videoasset
    frame = mover_frames(m, frames, plate, int((p.get("texture_meta") or {}).get("padding") or 0))
    first, last = int(p["first"]), int(p["last"])
    path, code = videoasset.encode_capped(lambda: (frame(f) for f in range(first, last + 1)), fps, out, alpha=True)
    return (str(path), None) if path is not None else (None, code)
