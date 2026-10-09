"""Plate v2 (after tracking): the temporal median over only the frames where a pixel is uncovered; pixels
no frame shows are filled from their surroundings and recorded as synthetic. Large elements animating in place
(movers) are then found and taken out of it. The plate is classified: flat colour, editable gradient (static,
or animated with keys), still image, or video: a plate that keeps moving gets a per-frame plate (the nearest frame
that shows each pixel, `stages/plate_frames.npy`) played as `assets/background.webm` with the median as its poster."""
from __future__ import annotations
import json, math, os, threading, time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
import cv2, numpy as np
from ..ir.colour import delta_e, hex_to_rgb8, rgb8_to_hex, srgb8_to_lab, srgb_to_lab
from ..ir.gradient import gradient_at, render_gradient
from ..ir.schema import Background, Gradient, GradientKey
from ..log import get
from . import videoasset
from .background import PLATE_PATH
from .gradient_fit import fit_gradient, fit_like, follow, measure, shrink, stop_range
from .text import reveal_exclusion_boxes

log = get("keepframe.analyze")

SAMPLES = 48
DILATE_PX = 8
OCR_PAD = 2
BAND = 64
SENTINEL = 256            # sorts after every 8-bit value, so covered samples collect at the end
RECOVER = 16              # unsampled frames kept per candidate pixel for its median
FLAT_P95 = 2.0            # ΔE76 vs the plate mean
POLY_MAX_RMS = 2.0        # ring RMS ΔE76 for the degree-2 fit
MIN_RING = 30
ENCLOSED_MAX = 0.25       # enclosed interiors up to this share of the frame count as covered
INPAINT_PAD = 32
INPAINT_RADIUS = 5
FALLBACK_CONF = 0.5
STATIC_P95 = 1.5          # temporal ΔE76 p95 below which the plate holds still
GRADIENT_P95 = 1.5        # a static plate within this of its fitted gradient (at fit size) is that gradient …
GRADIENT_FULL_P95 = 3.0   # … when it also stays within this at ≤ 640 px (R19, without its 3×3 blur)
MIN_STOP_RANGE = 3.0      # ΔE76 between the most different stops, else it is not a gradient
RANK2 = 0.95              # variance share of the top two temporal components of an animated gradient
ANIM_P95 = 3.0            # per-sample fit p95 of an animated gradient
ANIM_SHARE = 0.9          # share of samples that must fit
KEY_DE = 1.0              # a key is dropped when interpolating over it stays within this
MIN_SEEN = 3              # uncovered samples a pixel needs to count in the temporal statistics
TEMPORAL_PX = 20000       # pixels the temporal statistics look at, at most
ANIM_WORK = 64            # work size (px) of the per-sample fits
KEY_PX = 48               # render size (px) for the key-dropping comparison
MIN_VALID = 0.25          # a sample needs this share of uncovered pixels to be fitted
MOVER_PAD = 24            # the median is recomputed in the movers' bbox ± this
SYNTH_PATH = "assets/background_synthetic.png"
VIDEO_PATH = "assets/background.webm"
FRAMES_PATH = "stages/plate_frames.npy"
PLATE_JSON = "stages/plate.json"
VERSION = 2


@dataclass
class PlateModel:
    kind: str                         # color | gradient | image | video
    image: np.ndarray                 # H×W×3 uint8 still plate: the render of a static gradient, else the median (a
                                      # video's poster)
    rgb: tuple[int, int, int]         # flat colour, or the representative colour of a picture
    synthetic: np.ndarray | None      # H×W bool: filled, never-visible pixels
    confidence: float = 1.0
    stats: dict = field(default_factory=dict)
    gradient: Gradient | None = None
    gradient_keys: list[GradientKey] = field(default_factory=list)
    frames: np.ndarray | None = None  # per-frame plates of a video background (memmap of FRAMES_PATH)
    movers: list = field(default_factory=list)   # movers.Mover taken out of this plate (cached in stages/movers.pkl)
    _cache: dict = field(default_factory=dict, init=False, repr=False, compare=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False, compare=False)

    def at(self, f: int) -> np.ndarray:
        """The plate behind frame f: an animated gradient renders its key-interpolated gradient."""
        if self.frames is not None:
            return self.frames[f]
        if len(self.gradient_keys) < 2:
            return self.image
        with self._lock:
            hit = self._cache.get(f)
        if hit is None:
            H, W = self.image.shape[:2]
            hit = render_gradient(gradient_at(self, f), W, H)
            with self._lock:
                while len(self._cache) >= 8:
                    self._cache.pop(next(iter(self._cache)))
                self._cache[f] = hit
        return hit

    def crop(self, f: int, box) -> np.ndarray:
        x0, y0, x1, y1 = box
        return self.at(f)[y0:y1, x0:x1]


def _workers() -> int:
    return max(1, min(4, os.cpu_count() or 1))


def sample_frames(n: int, s: int = SAMPLES) -> list[int]:
    if n <= s:
        return list(range(n))
    return np.rint(np.linspace(0, n - 1, s)).astype(int).tolist()


def _kernel(px: int) -> np.ndarray:
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * px + 1, 2 * px + 1))


def _fill_enclosed(m: np.ndarray, max_area: float) -> None:
    """Mark free components (4-connected) that do not reach the frame edge and are at most max_area as covered."""
    n, lab, stats, _ = cv2.connectedComponentsWithStats((m == 0).astype(np.uint8), connectivity=4)
    if n <= 2:
        return
    fill = stats[:, cv2.CC_STAT_AREA] <= max_area
    fill[0] = False
    fill[np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]])] = False
    if fill.any():
        m[fill[lab]] = 1


def _occ_frame(shape, regions, boxes, reveal, dilate_px) -> np.ndarray:
    H, W = shape
    m = np.zeros((H, W), np.uint8)
    for r in regions:
        x0, y0, x1, y1 = r.bbox
        m[y0:y1, x0:x1] |= r.mask
    rects = [(b.bbox[0] - OCR_PAD, b.bbox[1] - OCR_PAD, b.bbox[2] + OCR_PAD, b.bbox[3] + OCR_PAD) for b in boxes]
    for x0, y0, x1, y1 in rects + list(reveal):
        m[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = 1
    if dilate_px:
        m = cv2.dilate(m, _kernel(dilate_px))
    _fill_enclosed(m, ENCLOSED_MAX * H * W)
    return m.astype(bool)


def occupancy(shape, rbf, boxes, reveal_boxes, frames, dilate_px=DILATE_PX) -> np.ndarray:
    """Per listed frame: region masks, OCR boxes (+2 px) and reveal boxes, dilated, with enclosed interiors
    (≤ 25 % of the frame) filled — pass 1 absorbs a large flat element's inside, so only its rim is a region.
    The raw difference to the pass-1 plate is left out on purpose: that plate holds the smears pass 2 removes."""
    out = np.empty((len(frames), *shape), bool)

    def run(j):
        f = frames[j]
        out[j] = _occ_frame(shape, rbf[f], boxes[f], reveal_boxes.get(f, ()), dilate_px)

    with ThreadPoolExecutor(_workers()) as ex:
        list(ex.map(run, range(len(frames))))
    return out


def _median_sorted(v: np.ndarray, c: np.ndarray) -> np.ndarray:
    """Median of the first c entries along axis 0 of sorted v, rounded half up (c == 0 → SENTINEL)."""
    lo = (np.maximum(c, 1) - 1) // 2
    hi = c // 2
    a = np.take_along_axis(v, lo[None, ..., None], 0)[0]
    b = np.take_along_axis(v, hi[None, ..., None], 0)[0]
    return (a + b + 1) >> 1


def masked_median(frames, sample, occ, band=BAND) -> tuple[np.ndarray, np.ndarray]:
    idx = np.asarray(sample)
    S, H, W = len(idx), frames.shape[1], frames.shape[2]
    plate = np.empty((H, W, 3), np.uint8)
    counts = np.empty((H, W), np.uint16)

    def run(y0):
        y1 = min(H, y0 + band)
        v = frames[idx, y0:y1].astype(np.uint16)
        o = occ[:, y0:y1]
        v[o] = SENTINEL
        v.sort(axis=0, kind="stable")   # radix sort for 16-bit
        c = S - o.sum(0, dtype=np.int32)
        m = _median_sorted(v, c)
        m[c == 0] = 0
        plate[y0:y1] = m
        counts[y0:y1] = c

    with ThreadPoolExecutor(_workers()) as ex:
        list(ex.map(run, range(0, H, band)))
    return plate, counts


def find_holes(counts, frames, rbf, boxes, reveal_boxes, *, skip=(), extra=None) -> tuple[np.ndarray, np.ndarray]:
    """Pixels no sample showed (count == 0) are checked against every frame (`skip`: frames known to cover
    them, i.e. the samples; `extra(f)`: more coverage, the movers). Returns the true holes and `uncovered`: the
    median of up to RECOVER frames that do show a candidate pixel (SENTINEL elsewhere)."""
    cand = counts == 0
    uncovered = np.full(counts.shape + (3,), SENTINEL, np.uint16)
    if not cand.any():
        return cand, uncovered
    pos = np.flatnonzero(cand)
    slots = np.full((RECOVER, len(pos), 3), SENTINEL, np.uint16)
    k = np.zeros(len(pos), np.int32)
    skip = set(skip)
    todo = [f for f in range(len(frames)) if f not in skip]

    def covered(f):
        m = _occ_frame(counts.shape, rbf[f], boxes[f], reveal_boxes.get(f, ()), DILATE_PX)
        return (m | extra(f) if extra is not None else m).ravel()[pos]

    with ThreadPoolExecutor(_workers()) as ex:
        for f, occ in zip(todo, ex.map(covered, todo)):
            free = ~occ & (k < RECOVER)
            if free.any():
                j = np.flatnonzero(free)
                slots[k[j], j] = frames[f].reshape(-1, 3)[pos[j]]
                k[j] += 1
    holes = cand.copy()
    seen = k > 0
    if seen.any():
        v = np.sort(slots[:, seen], axis=0, kind="stable")
        uncovered.reshape(-1, 3)[pos[seen]] = _median_sorted(v, k[seen])
        holes.ravel()[pos[seen]] = False
    return holes, uncovered


def _poly_fit(crop: np.ndarray, ring: np.ndarray, hole: np.ndarray) -> tuple[np.ndarray, float]:
    """Degree-2 least squares per channel on the ring; returns the hole's prediction and the ring RMS ΔE."""
    h, w = ring.shape
    s = max(h, w) / 2

    def design(ys, xs):
        u, v = (xs - w / 2) / s, (ys - h / 2) / s
        return np.stack([np.ones_like(u), u, v, u * u, u * v, v * v], 1)

    ys, xs = np.nonzero(ring)
    step = max(1, len(ys) // 20000)
    ys, xs = ys[::step], xs[::step]
    vals = crop[ys, xs].astype(np.float64)
    a = design(ys, xs)
    coef = np.linalg.lstsq(a, vals, rcond=None)[0]
    de = delta_e(srgb_to_lab(np.clip(a @ coef, 0, 255)), srgb_to_lab(vals))
    hy, hx = np.nonzero(hole)
    return np.clip(np.rint(design(hy, hx) @ coef), 0, 255).astype(np.uint8), float(np.sqrt(np.mean(de ** 2)))


def fill_holes(plate, holes, uncovered, *, poly_max_rms=POLY_MAX_RMS) -> tuple[np.ndarray, np.ndarray, list[dict]]:
    """Recovered pixels take their `uncovered` value; each hole takes a degree-2 fit of its ring
    dilate(r)∖dilate(2), r = clip(0.25·√area, 8, 48), when the ring RMS ΔE ≤ poly_max_rms, else Telea
    inpainting on its bbox ± 32. Returns the plate, the synthetic mask and one record per hole."""
    plate = plate.copy()
    got = uncovered[..., 0] < SENTINEL
    plate[got] = uncovered[got]
    synthetic = np.zeros(holes.shape, bool)
    info: list[dict] = []
    if not holes.any():
        return plate, synthetic, info
    if holes.all():
        raise ValueError("no frame shows any background pixel")
    H, W = holes.shape
    n, lab, stats, _ = cv2.connectedComponentsWithStats(holes.astype(np.uint8), connectivity=8)
    for k in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[k])
        r = int(np.clip(0.25 * np.sqrt(area), 8, 48))
        X0, Y0, X1, Y1 = max(0, x - r - 1), max(0, y - r - 1), min(W, x + w + r + 1), min(H, y + h + r + 1)
        comp = lab[Y0:Y1, X0:X1] == k
        d = cv2.distanceTransform((~comp).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
        ring = (d > 2) & (d <= r) & ~holes[Y0:Y1, X0:X1]
        method, rms = "inpaint", None
        if ring.sum() >= MIN_RING:
            pred, rms = _poly_fit(plate[Y0:Y1, X0:X1], ring, comp)
            if rms <= poly_max_rms:
                plate[Y0:Y1, X0:X1][comp] = pred
                method = "poly"
        if method == "inpaint":
            X0, Y0, X1, Y1 = max(0, x - INPAINT_PAD), max(0, y - INPAINT_PAD), min(W, x + w + INPAINT_PAD), min(H, y + h + INPAINT_PAD)
            todo = (holes & ~synthetic)[Y0:Y1, X0:X1]
            comp = lab[Y0:Y1, X0:X1] == k
            if todo.all():
                plate[Y0:Y1, X0:X1][comp] = np.rint(plate[~holes | synthetic].mean(0)).astype(np.uint8)
                method = "mean"
            else:
                out = cv2.inpaint(np.ascontiguousarray(plate[Y0:Y1, X0:X1]), todo.astype(np.uint8), INPAINT_RADIUS, cv2.INPAINT_TELEA)
                plate[Y0:Y1, X0:X1][comp] = out[comp]
        synthetic[y:y + h, x:x + w] |= lab[y:y + h, x:x + w] == k
        info.append({"bbox": [x, y, x + w, y + h], "area": area, "method": method,
                     "rms": None if rms is None else round(rms, 3)})
    return plate, synthetic, info


def _still_kind(plate, bg_rgb, pass1) -> tuple[str, tuple[int, int, int], float]:
    """color when the plate is flat (p95 ΔE vs its mean < FLAT_P95), else image. A plate pass 1 already
    judged flat keeps pass 1's colour."""
    px = plate[::2, ::2].reshape(-1, 3)
    mean = px.mean(0)
    p95 = float(np.percentile(delta_e(srgb_to_lab(px), srgb_to_lab(mean)), 95))
    rgb = tuple(int(round(v)) for v in mean)
    if p95 < FLAT_P95:
        return "color", (tuple(int(v) for v in bg_rgb) if pass1 is None else rgb), p95
    return "image", rgb, p95


def _low(img, size):
    return cv2.blur(cv2.resize(img, size, interpolation=cv2.INTER_AREA), (3, 3))


def temporal_stats(frames, sample, occ, median) -> dict:
    """How the plate moves. V = per-pixel RMS ΔE76 of the samples against the masked median at 1/4 size after
    a 3×3 blur, over pixels uncovered in ≥ MIN_SEEN samples; `p95_dE` is its 95th percentile. `rank2_ratio`:
    share of the mean-subtracted Lab variance over time held by the top two singular vectors (covered
    samples count as the pixel's mean)."""
    H, W = median.shape[:2]
    size = (max(1, round(W / 4)), max(1, round(H / 4)))
    k = max(1, math.ceil(math.sqrt(size[0] * size[1] / TEMPORAL_PX)))
    ref = srgb8_to_lab(_low(median, size)[::k, ::k])
    lab = np.empty((len(sample), *ref.shape), np.float32)
    free = np.empty((len(sample), *ref.shape[:2]), bool)
    ring = np.ones((3, 3), np.uint8)
    for j, f in enumerate(sample):
        lab[j] = srgb8_to_lab(_low(frames[f], size)[::k, ::k])
        cov = cv2.resize(occ[j].astype(np.uint8) * 255, size, interpolation=cv2.INTER_AREA)
        free[j] = cv2.dilate(cov, ring)[::k, ::k] == 0
    seen = free.sum(0)
    ok = seen >= MIN_SEEN
    if not ok.any():
        return {"p95_dE": 0.0, "rank2_ratio": 1.0, "pixels": 0}
    x, m, n = lab[:, ok], free[:, ok, None], seen[ok][:, None]
    v = np.sqrt((((x - ref[ok]) ** 2) * m).sum((0, 2)) / n[:, 0])
    x = np.where(m, x - (x * m).sum(0) / n, 0).reshape(len(sample), -1)
    s2 = np.clip(np.linalg.eigvalsh(x @ x.T), 0, None)[::-1]   # squared singular values
    r2 = float(s2[:2].sum() / s2.sum()) if s2.sum() > 1e-6 else 1.0
    return {"p95_dE": float(np.percentile(v, 95)), "rank2_ratio": r2, "pixels": int(ok.sum())}


def _drop_keys(keys: list[GradientKey], size) -> list[GradientKey]:
    """Drop every inner key whose removal keeps each fitted sample between its neighbours within KEY_DE
    (p95, rendered at KEY_PX)."""
    W, H = size
    s = KEY_PX / max(W, H)
    ys, xs = np.mgrid[0:max(1, round(H * s)), 0:max(1, round(W * s))].astype(np.float64)
    xy = ((xs + 0.5) * W / xs.shape[1], (ys + 0.5) * H / xs.shape[0])
    lab = lambda g: srgb8_to_lab(render_gradient(g, W, H, xy))
    want = {k.t: lab(k.gradient) for k in keys}
    kept, i = list(keys), 1
    while i < len(kept) - 1:
        pair = SimpleNamespace(gradient=None, gradient_keys=[kept[i - 1], kept[i + 1]])
        if all(np.percentile(delta_e(lab(gradient_at(pair, t)), want[t]), 95) <= KEY_DE
               for t in want if kept[i - 1].t < t < kept[i + 1].t):
            del kept[i]
        else:
            i += 1
    return kept


def _animated_keys(frames, sample, occ, size) -> tuple[list[GradientKey], float]:
    """A gradient per sample (at ANIM_WORK px; the first a full search, then warm-started with its kind and
    stop count) and the share of samples fitting within ANIM_P95. Keys are returned (then thinned) only when
    that share reaches ANIM_SHARE."""
    fits, like = [], None
    for j, f in enumerate(sample):
        small, valid = shrink(frames[f], ~occ[j], ANIM_WORK)
        if valid.mean() < MIN_VALID:
            continue
        g, p = fit_like(small, valid, size, like)
        if like is not None and p > ANIM_P95:
            g, p = fit_like(small, valid, size, like, warm=False)
        g = follow(g, like)
        fits.append((f, g, p))
        if g is not None and p <= ANIM_P95:
            like = g
    good = [(f, g) for f, g, p in fits if p <= ANIM_P95]
    share = len(good) / len(fits) if fits else 0.0
    if len(good) < 2 or share < ANIM_SHARE:
        return [], share
    return _drop_keys([GradientKey(t=int(f), gradient=g) for f, g in good], size), share


def classify(model: PlateModel, stats: dict, frames, sample, occ) -> PlateModel:
    """Static plate (temporal p95 < STATIC_P95): color when flat; gradient when a ≤ 4-stop linear or ≤ 3-stop
    radial gradient fits within GRADIENT_P95 at ≤ 160 px and GRADIENT_FULL_P95 at ≤ 640 px and its stops
    span ≥ MIN_STOP_RANGE; else image. Moving plate:
    gradient with keys when rank 2 over time and fitted per sample; else a video candidate: a still image with a
    message, until build_plate makes it a video plate."""
    p95, r2 = stats["p95_dE"], stats["rank2_ratio"]
    st = {**model.stats, "temporal_p95_de": round(p95, 3), "rank2_ratio": round(r2, 4)}
    H, W = model.image.shape[:2]
    if p95 < STATIC_P95:
        if model.kind == "color":
            return replace(model, stats=st)
        valid = ~model.synthetic if model.synthetic is not None and model.synthetic.any() else None
        g, gp = fit_gradient(model.image, valid)
        st["gradient_p95_de"] = round(gp, 3) if math.isfinite(gp) else None
        if g is not None and gp <= GRADIENT_P95 and stop_range(g) >= MIN_STOP_RANGE:
            full = measure(g, model.image, valid)   # the ≤ 160 px fit must not have erased grain or patterns
            st["gradient_full_p95_de"] = round(full, 3)
            if full <= GRADIENT_FULL_P95:
                return replace(model, kind="gradient", image=render_gradient(g, W, H), gradient=g, stats=st)
        return replace(model, kind="image", stats=st)
    keys, share = _animated_keys(frames, sample, occ, (W, H)) if r2 >= RANK2 else ([], 0.0)
    st["animated_fit_share"] = round(share, 3)
    if keys:
        return replace(model, kind="gradient", gradient=keys[0].gradient, gradient_keys=keys, stats=st)
    st["video_candidate"] = True
    st["message"] = f"background animates (p95 ΔE {p95:.1f}); kept as a still image"
    return replace(model, kind="image", stats=st)


def _video_plate(model: PlateModel, frames, rbf, boxes, reveal, out) -> PlateModel:
    """A video candidate as a video plate: every frame's occupancy (movers included) in a temporary memmap, then
    fill_video_plate behind the still plate. Without a VP9 encoder, or on any error, the still image and its message."""
    if not videoasset.ffmpeg_vp9_ok():
        return model
    from .movers import mover_cover
    t0 = time.perf_counter()
    n, H, W = frames.shape[:3]
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    occ_path = out.with_name(".plate_occ.npy")
    try:
        occ = np.lib.format.open_memmap(occ_path, mode="w+", dtype=bool, shape=(n, H, W))

        def run(f):
            m = _occ_frame((H, W), rbf[f], boxes[f], reveal.get(f, ()), DILATE_PX)
            occ[f] = m | mover_cover(model.movers, f, (H, W)) if model.movers else m

        with ThreadPoolExecutor(_workers()) as ex:
            list(ex.map(run, range(n)))
        per_frame = videoasset.fill_video_plate(frames, occ, model.image, out)
    except Exception:   # fail soft: the still image keeps its message
        log.exception("video plate failed")
        out.unlink(missing_ok=True)
        return model
    finally:
        occ_path.unlink(missing_ok=True)
    st = {k: v for k, v in model.stats.items() if k != "message"}
    log.info("plate video frames %.2fs n=%s", time.perf_counter() - t0, n)
    return replace(model, kind="video", frames=per_frame, stats={**st, "video": True})


def write_video(sd, model: PlateModel, fps: float) -> PlateModel:
    """VIDEO_PATH from a video plate's frames (size-capped). When it cannot be made: the still image with a message
    naming the code, and the per-frame plate is removed."""
    if model.kind != "video":
        return model
    sd = Path(sd)
    t0 = time.perf_counter()
    n = len(model.frames)
    path, code = videoasset.encode_capped(lambda: (model.frames[f] for f in range(n)), fps, sd / VIDEO_PATH, alpha=False)
    if path is None:
        p95 = float(model.stats.get("temporal_p95_de") or 0.0)
        st = {**model.stats, "video": False,
              "message": f"background animates (p95 ΔE {p95:.1f}); kept as a still image ({code})"}
        (sd / FRAMES_PATH).unlink(missing_ok=True)
        return replace(model, kind="image", frames=None, stats=st)
    size = path.stat().st_size
    log.info("plate video encode %.2fs bytes=%s", time.perf_counter() - t0, size)
    return replace(model, stats={**model.stats, "video_bytes": size})


def _without_movers(frames, sample, occ, raw, counts, movers, rbf, boxes, reveal):
    """Add the mover masks to the occupancy, recompute the median in their bbox ± MOVER_PAD, refill the holes."""
    from .movers import mover_cover
    H, W = counts.shape
    bb = np.array([b for m in movers for b, _ in m.frames.values()])
    x0, y0 = max(0, int(bb[:, 0].min()) - MOVER_PAD), max(0, int(bb[:, 1].min()) - MOVER_PAD)
    x1, y1 = min(W, int(bb[:, 2].max()) + MOVER_PAD), min(H, int(bb[:, 3].max()) + MOVER_PAD)
    win = occ[:, y0:y1, x0:x1].copy()
    for j, f in enumerate(sample):
        win[j] |= mover_cover(movers, f, (H, W))[y0:y1, x0:x1]
    sub, sub_counts = masked_median(frames[:, y0:y1, x0:x1], sample, win)
    raw, counts = raw.copy(), counts.copy()
    raw[y0:y1, x0:x1], counts[y0:y1, x0:x1] = sub, sub_counts
    holes, uncovered = find_holes(counts, frames, rbf, boxes, reveal, skip=sample,
                                  extra=lambda f: mover_cover(movers, f, (H, W)))
    out = fill_holes(raw, holes, uncovered)
    occ[:, y0:y1, x0:x1] = win   # the temporal statistics no longer see the movers either
    return out


def _movers(frames, sample, occ, raw, counts, plate, rbf, boxes, reveal, text_tracks, shape_tracks, obj_tracks):
    """The movers in this shot and the plate without them (None when there are none)."""
    from .movers import established_mask, find_movers, instability
    n, H, W = frames.shape[:3]
    est = established_mask((H, W), text_tracks, obj_tracks, n, sample)
    u = instability(frames, sample, est)
    movers = find_movers(frames, sample, u, obj_tracks, shape_tracks, plate=plate, text_tracks=text_tracks, established=est)
    del est
    if not movers:
        return [], None
    return movers, _without_movers(frames, sample, occ, raw, counts, movers, rbf, boxes, reveal)


def build_plate(frames, rbf, boxes, text_tracks, shape_tracks, *, bg_rgb, bconf, bg_override, pass1,
                obj_tracks=None, frames_out=None) -> PlateModel:
    """`obj_tracks` (the tracking output) enables the mover search; a plate that is already flat has none.
    `frames_out` (FRAMES_PATH) enables video plates."""
    n, H, W = frames.shape[:3]
    if bg_override:
        rgb = hex_to_rgb8(bg_override)
        return PlateModel("color", np.full((H, W, 3), rgb, np.uint8), rgb, None, float(bconf), {"override": True})
    t = [time.perf_counter()]
    reveal = reveal_exclusion_boxes(list(text_tracks) + list(shape_tracks))
    sample = sample_frames(n)
    occ = occupancy((H, W), rbf, boxes, reveal, sample)
    t.append(time.perf_counter())
    raw, counts = masked_median(frames, sample, occ)
    t.append(time.perf_counter())
    holes, uncovered = find_holes(counts, frames, rbf, boxes, reveal, skip=sample)
    t.append(time.perf_counter())
    plate, synthetic, filled = fill_holes(raw, holes, uncovered)
    t.append(time.perf_counter())
    kind, rgb, p95 = _still_kind(plate, bg_rgb, pass1)
    movers, mover_msg, search = [], None, obj_tracks is not None and kind != "color"
    if search:
        try:
            movers, without = _movers(frames, sample, occ, raw, counts, plate, rbf, boxes, reveal, text_tracks,
                                      shape_tracks, obj_tracks)
            if without is not None:
                plate, synthetic, filled = without
                kind, rgb, p95 = _still_kind(plate, bg_rgb, pass1)
        except Exception as e:   # fail soft: keep the plate without the mover search, at lower confidence
            log.exception("mover search failed")
            movers, mover_msg = [], f"movers skipped: {type(e).__name__}: {e}"[:200]
        log.info("plate movers %.2fs count=%s", time.perf_counter() - t[-1], len(movers))
    t.append(time.perf_counter())
    frac = float(synthetic.mean())
    stats = {"samples": len(sample), "holes": filled, "synthetic_fraction": frac, "p95_flat_de": round(p95, 3)}
    if search:
        stats.update({"movers": len(movers)} if mover_msg is None else {"movers_message": mover_msg})
    model, failed = PlateModel(kind, plate, rgb, synthetic, float(bconf), stats, movers=movers), False
    try:
        model = classify(model, temporal_stats(frames, sample, occ, plate), frames, sample, occ)
    except Exception as e:   # fail soft: keep the still kind, at lower confidence, and say so
        log.exception("plate classification failed")
        model.stats["message"] = f"plate classification skipped: {type(e).__name__}: {e}"[:200]
        failed = True
    if model.stats.get("video_candidate") and frames_out is not None:
        model = _video_plate(model, frames, rbf, boxes, reveal, frames_out)
    t.append(time.perf_counter())
    if model.kind == "color":   # a flat colour never shows the filled pixels
        model.synthetic = None
    else:
        model.confidence *= 1 - 0.5 * frac
    if failed or mover_msg:
        model.confidence *= FALLBACK_CONF
    secs = dict(zip(("occupancy", "median", "holes", "fill", "movers", "classify"), (round(b - a, 3) for a, b in zip(t, t[1:]))))
    model.stats["seconds"] = secs
    log.info("plate classify %.2fs kind=%s temporal_p95=%s rank2=%s gradient_p95=%s keys=%s", t[-1] - t[-2], model.kind,
             model.stats.get("temporal_p95_de"), model.stats.get("rank2_ratio"), model.stats.get("gradient_p95_de"),
             len(model.gradient_keys))
    log.info("plate pass2 %.2fs samples=%s holes=%s synthetic=%.4f kind=%s %s", t[-1] - t[0], len(sample), len(filled),
             frac, model.kind, secs)
    return model


def fallback_plate(pass1, bg_rgb, bconf, shape, message: str) -> PlateModel:
    """Today's plate (pass 1, or the estimated colour) at lower confidence, when pass 2 fails."""
    H, W = shape
    conf, stats = float(bconf) * FALLBACK_CONF, {"message": message}
    rgb = tuple(int(v) for v in bg_rgb)
    if pass1 is None:
        return PlateModel("color", np.full((H, W, 3), rgb, np.uint8), rgb, None, conf, stats)
    return PlateModel("image", pass1, rgb, np.zeros((H, W), bool), conf, stats)


def _write_png(path: Path, img: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(path), img):
        raise OSError(f"could not write {path}")


def save_plate(sd, model: PlateModel) -> None:
    sd = Path(sd)
    H, W = model.image.shape[:2]
    synth = model.kind != "color" and model.synthetic is not None and bool(model.synthetic.any())
    if model.kind != "color":
        _write_png(sd / PLATE_PATH, cv2.cvtColor(model.image, cv2.COLOR_RGB2BGR))
    if synth:
        _write_png(sd / SYNTH_PATH, model.synthetic.astype(np.uint8) * 255)
    path = sd / PLATE_JSON
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"version": VERSION, "kind": model.kind, "rgb": list(model.rgb), "confidence": model.confidence,
                               "size": [W, H], "synthetic": synth, "stats": model.stats,
                               "gradient": model.gradient.model_dump(mode="json") if model.gradient is not None else None,
                               "gradient_keys": [k.model_dump(mode="json") for k in model.gradient_keys]}))
    os.replace(tmp, path)


def load_plate(sd) -> PlateModel | None:
    """The cached plate, or None when it must be rebuilt: legacy project, other version, corrupt or partial
    plate.json, missing or mismatched image."""
    sd = Path(sd)
    try:
        j = json.loads((sd / PLATE_JSON).read_text())
        if j.get("version") != VERSION:
            return None
        kind, (W, H), rgb = j["kind"], (int(v) for v in j["size"]), tuple(int(v) for v in j["rgb"])
        confidence, stats, synth = float(j["confidence"]), j.get("stats", {}), bool(j.get("synthetic"))
        gradient = Gradient.model_validate(j["gradient"]) if j.get("gradient") is not None else None
        keys = [GradientKey.model_validate(k) for k in j.get("gradient_keys") or []]
        if kind not in ("color", "image", "gradient", "video") or len(rgb) != 3 or not all(0 <= v <= 255 for v in rgb) \
                or W <= 0 or H <= 0 or not isinstance(stats, dict) or (kind == "gradient" and gradient is None):
            return None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    if kind == "color":
        return PlateModel("color", np.full((H, W, 3), rgb, np.uint8), rgb, None, confidence, stats)
    img = cv2.imread(str(sd / PLATE_PATH), cv2.IMREAD_COLOR)
    if img is None or img.shape[:2] != (H, W):
        return None
    synthetic = np.zeros((H, W), bool)
    if synth:
        m = cv2.imread(str(sd / SYNTH_PATH), cv2.IMREAD_GRAYSCALE)
        if m is None or m.shape != (H, W):
            return None
        synthetic = m > 127
    frames = None
    if kind == "video":   # the per-frame plate and its WebM must both be there
        try:
            frames = np.load(sd / FRAMES_PATH, mmap_mode="r")
        except (OSError, ValueError):
            return None
        if frames.ndim != 4 or frames.shape[1:] != (H, W, 3) or not (sd / VIDEO_PATH).is_file():
            return None
    return PlateModel(kind, cv2.cvtColor(img, cv2.COLOR_BGR2RGB), rgb, synthetic, confidence, stats, gradient, keys, frames)


def background_for(model: PlateModel, sd) -> Background:
    if model.kind == "color":
        return Background(kind="color", value=rgb8_to_hex(model.rgb), confidence=model.confidence)
    if model.kind == "gradient":   # editable; the poster (render, or median of an animated one) is for stills and AE
        poster = PLATE_PATH if (Path(sd) / PLATE_PATH).is_file() else None
        return Background(kind="gradient", value=rgb8_to_hex(model.rgb), confidence=model.confidence, gradient=model.gradient,
                          gradient_keys=list(model.gradient_keys), poster=poster)
    synth = model.synthetic is not None and model.synthetic.any() and (Path(sd) / SYNTH_PATH).is_file()
    if model.kind == "video":   # plays the WebM; the median is the poster (stills, AE until Task 14)
        return Background(kind="video", value=VIDEO_PATH, poster=PLATE_PATH, confidence=model.confidence,
                          synthetic=SYNTH_PATH if synth else None)
    return Background(kind="image", value=PLATE_PATH, confidence=model.confidence, synthetic=SYNTH_PATH if synth else None)
