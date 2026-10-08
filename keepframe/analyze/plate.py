"""Plate v2 (after tracking): the temporal median over only the frames where a pixel is uncovered; pixels
no frame shows are filled from their surroundings and recorded as synthetic."""
from __future__ import annotations
import json, os, time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
import cv2, numpy as np
from ..ir.colour import delta_e, hex_to_rgb8, rgb8_to_hex, srgb_to_lab
from ..ir.schema import Background
from ..log import get
from .background import PLATE_PATH
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
SYNTH_PATH = "assets/background_synthetic.png"
PLATE_JSON = "stages/plate.json"
VERSION = 2


@dataclass
class PlateModel:
    kind: str                         # color | image (gradient / video later)
    image: np.ndarray                 # H×W×3 uint8 still plate
    rgb: tuple[int, int, int]         # flat colour, or the representative colour of a picture
    synthetic: np.ndarray | None      # H×W bool: filled, never-visible pixels
    confidence: float = 1.0
    stats: dict = field(default_factory=dict)
    gradient: object | None = None
    gradient_keys: list = field(default_factory=list)
    frames: np.ndarray | None = None  # per-frame plates of an animated background

    def at(self, f: int) -> np.ndarray:
        return self.image if self.frames is None else self.frames[f]

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


def find_holes(counts, frames, rbf, boxes, reveal_boxes, *, skip=()) -> tuple[np.ndarray, np.ndarray]:
    """Pixels no sample showed (count == 0) are checked against every frame (`skip`: frames known to cover
    them, i.e. the samples). Returns the true holes and `uncovered`: the median of up to RECOVER frames that
    do show a candidate pixel (SENTINEL elsewhere)."""
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
        return _occ_frame(counts.shape, rbf[f], boxes[f], reveal_boxes.get(f, ()), DILATE_PX).ravel()[pos]

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


def build_plate(frames, rbf, boxes, text_tracks, shape_tracks, *, bg_rgb, bconf, bg_override, pass1) -> PlateModel:
    n, H, W = frames.shape[:3]
    if bg_override:
        rgb = hex_to_rgb8(bg_override)
        return PlateModel("color", np.full((H, W, 3), rgb, np.uint8), rgb, None, float(bconf), {"override": True})
    t = [time.perf_counter()]
    reveal = reveal_exclusion_boxes(list(text_tracks) + list(shape_tracks))
    sample = sample_frames(n)
    occ = occupancy((H, W), rbf, boxes, reveal, sample)
    t.append(time.perf_counter())
    plate, counts = masked_median(frames, sample, occ)
    t.append(time.perf_counter())
    holes, uncovered = find_holes(counts, frames, rbf, boxes, reveal, skip=sample)
    t.append(time.perf_counter())
    plate, synthetic, filled = fill_holes(plate, holes, uncovered)
    t.append(time.perf_counter())
    kind, rgb, p95 = _still_kind(plate, bg_rgb, pass1)
    t.append(time.perf_counter())
    frac = float(synthetic.mean())
    secs = dict(zip(("occupancy", "median", "holes", "fill", "classify"), (round(b - a, 3) for a, b in zip(t, t[1:]))))
    log.info("plate pass2 %.2fs samples=%s holes=%s synthetic=%.4f kind=%s %s", t[-1] - t[0], len(sample), len(filled),
             frac, kind, secs)
    stats = {"samples": len(sample), "holes": filled, "synthetic_fraction": frac, "p95_flat_de": round(p95, 3), "seconds": secs}
    if kind == "color":   # a flat colour never shows the filled pixels
        return PlateModel(kind, plate, rgb, None, float(bconf), stats)
    return PlateModel(kind, plate, rgb, synthetic, float(bconf) * (1 - 0.5 * frac), stats)


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
                               "size": [W, H], "synthetic": synth, "stats": model.stats}))
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
        if kind not in ("color", "image") or len(rgb) != 3 or not all(0 <= v <= 255 for v in rgb) \
                or W <= 0 or H <= 0 or not isinstance(stats, dict):
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
    return PlateModel(kind, cv2.cvtColor(img, cv2.COLOR_BGR2RGB), rgb, synthetic, confidence, stats)


def background_for(model: PlateModel, sd) -> Background:
    if model.kind == "color":
        return Background(kind="color", value=rgb8_to_hex(model.rgb), confidence=model.confidence)
    synth = model.synthetic is not None and model.synthetic.any() and (Path(sd) / SYNTH_PATH).is_file()
    return Background(kind="image", value=PLATE_PATH, confidence=model.confidence, synthetic=SYNTH_PATH if synth else None)
