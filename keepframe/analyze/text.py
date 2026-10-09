from __future__ import annotations
import difflib, math, os
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Lock, local
from typing import Protocol
import cv2, numpy as np
from ..ir.colour import delta_e, srgb8_to_lab
from ..ir.schema import FontGuess
from ..log import get
from .background import foreground_mask, foreground_mask_plate, opacity_against_plate, rgb_to_lab
from .device import ocr_cuda, ocr_cuda_expected

log = get("keepframe.analyze")

MIN_TEXT_FRAMES = 3
CONFIDENT_OCR = 0.9
FULL_OPACITY = 0.97     # a text frame at this share of the peak level is at full opacity (0: off, the largest box) …
PEAK_SUPPORT = 3        # … the peak: the level this many frames reach (one or two bright frames are not the peak) …
FIT_DE = 8.0            # … over the frames whose glyph core is the text's colour faded over the plate, within this ΔE76
PEAK_PX = 1000          # core pixels per frame the measure keeps (evenly spaced)
_warned_ocr_cap_unavailable = False

# ponytail: greedy tracking + colour-threshold stroke masks. Upgrade path: frozen image spotter + light tracker (GoMatching++),
# DTW copy alignment (Haraguchi et al. 2022), Hi-SAM stroke masks.


@dataclass
class TextBox:
    frame: int
    text: str
    bbox: tuple[int, int, int, int]
    conf: float


class Ocr(Protocol):
    def __call__(self, frame_rgb: np.ndarray) -> list[tuple[str, tuple[int, int, int, int], float]]: ...


def _cap_ort_cuda_arena(limit: int) -> None:
    """Keep OCR from growing a CUDA arena that starves sprite refine."""
    try:
        from rapidocr_onnxruntime.utils.infer_engine import OrtInferSession
    except Exception:
        return
    if getattr(OrtInferSession, "_keepframe_capped", False):
        return

    orig = OrtInferSession._get_ep_list

    def _get_ep_list(self):
        out = []
        for name, opts in orig(self):
            if name == "CUDAExecutionProvider":
                opts = _cuda_provider_opts(opts, limit)
            out.append((name, opts))
        return out

    OrtInferSession._get_ep_list = _get_ep_list
    OrtInferSession._keepframe_capped = True


def _cuda_provider_opts(opts: dict, limit: int) -> dict:
    # Keep RapidOCR's cudnn_conv_algo_search (EXHAUSTIVE). ORT 1.20+ maps DEFAULT to
    # cuDNN Fallback and warns "OP Conv running in Fallback mode. May be extremely slow."
    out = dict(opts)
    out["gpu_mem_limit"] = int(limit)
    out["arena_extend_strategy"] = "kSameAsRequested"
    return out


def _ocr_gpu_mem_limit() -> int:
    try:
        import torch
        if torch.cuda.is_available():
            total = torch.cuda.get_device_properties(0).total_memory
            return int(min(4 * 1024 ** 3, max(1024 ** 3, total * 0.08)))
    except Exception:
        pass
    return 2 * 1024 ** 3


class RapidOcr:
    def __init__(self, max_side: int | None = None):
        if max_side is not None and max_side <= 0:
            raise ValueError("max_side must be positive or None")
        self.max_side = max_side
        from .device import preload_torch_cuda
        preload_torch_cuda()
        from rapidocr_onnxruntime import RapidOCR  # lazy import
        use_cuda = ocr_cuda()
        if use_cuda:
            self.workers = 1
            limit = _ocr_gpu_mem_limit()
            _cap_ort_cuda_arena(limit)
            log.info("ocr device=cuda gpu_mem_limit=%s", limit)
            kw = dict(det_use_cuda=True, cls_use_cuda=True, rec_use_cuda=True)
        else:
            # ponytail: four-worker ceiling; tune CPU/memory budgets for larger hosts.
            try:
                cores = len(os.sched_getaffinity(0))
            except (AttributeError, OSError):
                cores = os.cpu_count() or 1
            self.workers = min(4, cores)
            if ocr_cuda_expected():
                log.warning(
                    "ocr on cpu: CUDAExecutionProvider missing "
                    "(pip uninstall -y onnxruntime onnxruntime-gpu && pip install 'onnxruntime-gpu>=1.19,<1.27')"
                )
            else:
                log.info("ocr device=cpu")
            # One inference thread per frame worker avoids oversubscribing the CPU.
            # RapidOCR propagates these global parameters to all three ORT sessions.
            kw = dict(intra_op_num_threads=1, inter_op_num_threads=1)
        if max_side is not None:
            # RapidOCR 1.4.4 ignores det_limit_side_len for limit_type="max".
            # Prevent detector upscaling; cap its input explicitly below.
            kw["det_limit_type"] = "max"
        self._engine_kwargs = kw
        self._ocr = RapidOCR(**kw)

    def fork(self) -> RapidOcr:
        """Give each frame worker its own mutable RapidOCR pipeline and sessions."""
        worker = RapidOcr.__new__(RapidOcr)
        worker.max_side, worker.workers = self.max_side, self.workers
        worker._engine_kwargs = self._engine_kwargs.copy()
        worker._ocr = type(self._ocr)(**worker._engine_kwargs)
        return worker

    def __call__(self, frame_rgb: np.ndarray):
        global _warned_ocr_cap_unavailable
        bgr = frame_rgb[..., ::-1]  # expects BGR
        h, w = bgr.shape[:2]
        capped = self.max_side is not None and max(h, w) > self.max_side
        if capped and not all(hasattr(self._ocr, name) for name in
                              ("get_crop_img_list", "text_cls", "text_rec", "get_final_res")):
            if not _warned_ocr_cap_unavailable:
                _warned_ocr_cap_unavailable = True
                log.warning("ocr max_side ignored: RapidOCR staged API unavailable; using full-frame OCR")
            capped = False
        if not capped:
            result, _ = self._ocr(bgr)
        else:
            # ponytail: staged RapidOCR 1.x API; use native detection limits once they honor the cap.
            scale = self.max_side / max(h, w)
            small = cv2.resize(bgr, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=cv2.INTER_AREA)
            quads, times = self._ocr(small, use_cls=False, use_rec=False)
            if not quads:
                return []
            boxes = np.asarray(quads, np.float64)
            boxes[..., 0] *= w / small.shape[1]
            boxes[..., 1] *= h / small.shape[0]
            boxes = boxes.astype(np.float32)
            crops = self._ocr.get_crop_img_list(bgr, boxes)
            cls_res, cls_time = None, 0.0
            if self._ocr.use_cls:
                crops, cls_res, cls_time = self._ocr.text_cls(crops)
            rec_res, rec_time = self._ocr.text_rec(crops)
            result, _ = self._ocr.get_final_res(boxes, cls_res, rec_res, times[0], cls_time, rec_time)
        out = []
        for quad, text, conf in result or []:
            xs = [p[0] for p in quad]; ys = [p[1] for p in quad]
            out.append((text, (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))), float(conf)))
        return out


def ocr_frames(frames: np.ndarray, ocr: Ocr, step: int = 1, *,
               workers: int | None = None) -> list[list[TextBox]]:
    """Reuse exact consecutive samples; consume concurrent results in frame order.

    Engines with ``fork`` get an independent instance per worker. Other callables
    stay sequential by default and must be thread safe when requesting workers.
    At most twice the worker count of distinct sample futures wait to be consumed.
    """
    from keepframe.progress import report_stage
    workers = getattr(ocr, "workers", 1) if workers is None else workers
    if workers < 1:
        raise ValueError("workers must be positive")
    out: list[list[TextBox]] = []
    n = len(frames)
    mark = max(1, n // 10)

    def progress(i):
        if i == 0 or i + 1 == n or (i + 1) % mark == 0:
            report_stage("text", f"{i + 1}/{n}")

    worker = local()
    first_engine = [ocr]
    engine_lock = Lock()

    def start_worker():
        # RapidOCR's detector mutates preprocess_op: never share it across workers.
        with engine_lock:
            engine = first_engine.pop() if first_engine else None
        worker.ocr = engine if engine is not None else getattr(ocr, "fork", lambda: ocr)()

    def evaluate(frame):
        # Initialize inside the task so fork failures retain their original exception.
        if not hasattr(worker, "ocr"):
            start_worker()
        return worker.ocr(frame)

    def consume(start, stop, future):
        # Equal samples share a future; retain just the start of each result's run.
        result = None
        for i in range(start, stop):
            progress(i)
            if i % step == 0:
                if result is None:
                    result = future.result()
                out.append([TextBox(i, t, b, c) for t, b, c in result])
            else:
                out.append([])

    pool = ThreadPoolExecutor(max_workers=workers) if workers > 1 and n else None
    try:
        pending = deque()
        previous_frame, previous_result = None, None
        for i, frame in enumerate(frames):
            if pool is None:
                progress(i)
            result = None
            if i % step == 0:
                # No tolerance: even a one-channel, one-pixel change runs the engine.
                if previous_frame is None or not np.array_equal(frame, previous_frame):
                    previous_frame = frame
                    previous_result = pool.submit(evaluate, frame) if pool is not None else ocr(frame)
                    if pool is not None:
                        pending.append((i, previous_result))
                        if len(pending) >= 2 * workers:
                            start, future = pending.popleft()
                            consume(start, pending[0][0], future)
                result = previous_result
            if pool is None:
                out.append([TextBox(i, t, b, c) for t, b, c in result] if result is not None else [])
        while pending:
            start, future = pending.popleft()
            consume(start, pending[0][0] if pending else n, future)
        if pool is not None:
            pool.shutdown(wait=True)
    except BaseException:
        if pool is not None:
            # Running calls must finish; queued calls should never start after failure.
            pool.shutdown(wait=True, cancel_futures=True)
        raise
    return out


@dataclass
class TextTrack:
    id: int
    boxes: dict[int, TextBox] = field(default_factory=dict)
    text: str = ""
    reveal: bool = False

    @property
    def first(self) -> int:
        return min(self.boxes)

    @property
    def last(self) -> int:
        return max(self.boxes)


def keep_text_track(t: TextTrack) -> bool:
    """OCR on shapes yields 1–2 frame tracks and lone glyphs (O, 0, ·); keep real copy.
    ponytail: frame-count + alphanumeric heuristic; a text/non-text classifier if real clips still leak junk."""
    if len(t.boxes) < MIN_TEXT_FRAMES:
        return False
    alnum = sum(ch.isalnum() for ch in t.text)
    if alnum >= 2:
        return True
    if alnum == 0:
        return False
    return float(np.mean([b.conf for b in t.boxes.values()])) >= CONFIDENT_OCR


def split_junk_text(tracks: list[TextTrack]) -> tuple[list[TextTrack], list[TextTrack], int]:
    text_tracks, shape_tracks = [], []
    for t in tracks:
        (text_tracks if keep_text_track(t) else shape_tracks).append(t)
    return text_tracks, shape_tracks, len(shape_tracks)


def _iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0])); iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def _containment(a, b) -> float:
    inter = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    area = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return inter / area if area > 0 else 0.0


def _centre(b) -> tuple[float, float]:
    return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)


def _sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _compact(text: str) -> str:
    return "".join(text.split()).casefold()


def _reveal_aligned(a: TextBox, b: TextBox) -> bool:
    # The shorter box sets the tolerance, so a zoom cannot relax alignment.
    height = min(a.bbox[3] - a.bbox[1], b.bbox[3] - b.bbox[1])
    return height > 0 and abs(a.bbox[0] - b.bbox[0]) <= 0.25 * height and abs(a.bbox[3] - b.bbox[3]) <= 0.3 * height


def _widest_box(track: TextTrack) -> TextBox:
    return max(track.boxes.values(), key=lambda box: box.bbox[2] - box.bbox[0])


def merge_reveals(tracks: list[TextTrack], max_gap: int = 3) -> list[TextTrack]:
    """Fold aligned prefixes into the later track, retaining each frame's OCR box."""
    # ponytail: left-to-right reveals only; right/centre reveals stay separate tracks
    # Upgrade path: infer the reveal direction and expose it in the IR.
    for track in tracks:
        full = _widest_box(track)
        full_text = _compact(full.text)
        boxes = sorted(track.boxes.values(), key=lambda box: box.frame)
        widths = np.array([box.bbox[2] - box.bbox[0] for box in boxes])
        full_width = full.bbox[2] - full.bbox[0]
        tolerance = 0.05 * full_width
        # Include repeated widest observations so narrow/full OCR flicker cannot look like growth.
        growing_widths = widths[:np.flatnonzero(widths == full_width)[-1] + 1]
        monotonic = np.all(growing_widths[1:] >= np.maximum.accumulate(growing_widths)[:-1] - tolerance)
        narrower = [box for box, width in zip(boxes, widths) if width < full_width]
        if (widths[0] < full_width and monotonic
                and all(_compact(box.text) and full_text.startswith(_compact(box.text)) for box in narrower)
                and any(box.frame < full.frame and _compact(box.text) != full_text and _reveal_aligned(box, full)
                        for box in narrower)):
            track.reveal = True
            track.text = full.text
    remaining = sorted(tracks, key=lambda track: track.first)
    while True:
        merged = False
        for i, earlier in enumerate(remaining):
            prefix = _compact(earlier.text)
            for later in remaining[i + 1:]:
                text = _compact(later.text)
                if not prefix or prefix == text or not text.startswith(prefix):
                    continue
                if abs(earlier.last - later.first) > max_gap:
                    continue
                if not _reveal_aligned(earlier.boxes[earlier.last], later.boxes[later.first]):
                    continue
                later.boxes = {**earlier.boxes, **later.boxes}
                later.reveal = True
                later.text = _widest_box(later).text
                remaining.remove(earlier)
                remaining.sort(key=lambda track: track.first)
                merged = True
                break
            if merged:
                break
        if not merged:
            return remaining


def reveal_exclusion_boxes(tracks: list[TextTrack]) -> dict[int, list[tuple[int, int, int, int]]]:
    """Exclude the full reveal box throughout its interval, including OCR gaps."""
    boxes: dict[int, list[tuple[int, int, int, int]]] = {}
    for track in tracks:
        if getattr(track, "reveal", False):
            full = _widest_box(track).bbox
            for frame in range(track.first, track.last + 1):
                boxes.setdefault(frame, []).append(full)
    return boxes


def track_text(boxes_by_frame: list[list[TextBox]], first_frame: int = 0, iou_thr: float = 0.3,
               max_dist: float = 60.0, max_gap: int = 3) -> list[TextTrack]:
    tracks: list[TextTrack] = []
    next_id = 1
    for i, boxes in enumerate(boxes_by_frame):
        f = first_frame + i
        active = [t for t in tracks if f - t.last <= max_gap]
        used: set[int] = set()
        for box in boxes:
            best, best_s = None, 0.0
            for t in active:
                if t.id in used:
                    continue
                last = t.boxes[t.last]
                geo = max(_iou(last.bbox, box.bbox), _containment(last.bbox, box.bbox),
                          1 - math.dist(_centre(last.bbox), _centre(box.bbox)) / max_dist)
                a, b = _compact(last.text), _compact(box.text)
                prefix = bool(a and b) and (a.startswith(b) or b.startswith(a))
                if geo < iou_thr or (_sim(last.text, box.text) < 0.5 and not prefix):
                    continue
                if geo > best_s:
                    best, best_s = t, geo
            if best is None:
                best = TextTrack(id=next_id); next_id += 1; tracks.append(best)
            best.boxes[f] = box; used.add(best.id)
    for t in tracks:
        t.text = Counter(b.text for b in t.boxes.values()).most_common(1)[0][0]
    return tracks


def apply_copy(tracks: list[TextTrack], copy: list[str]) -> None:
    j = 0
    for t in sorted(tracks, key=lambda t: t.first):
        best_k, best_s = None, 0.0
        for k in range(j, len(copy)):
            s = _sim(t.text, copy[k])
            if s > best_s:
                best_k, best_s = k, s
        if best_k is not None and best_s >= 0.6:
            t.text = copy[best_k]; j = best_k + 1


def text_exclusion_mask(boxes: list[TextBox], shape: tuple[int, int], pad: int = 2, *,
                        extra_boxes: list[tuple[int, int, int, int]] | None = None) -> np.ndarray:
    m = np.zeros(shape, bool)
    for x0, y0, x1, y1 in [b.bbox for b in boxes] + (extra_boxes or []):
        m[max(0, y0 - pad):y1 + pad, max(0, x0 - pad):x1 + pad] = True
    return m


def stroke_mask(frame: np.ndarray, bbox, bg_rgb: tuple, thr: float = 12.0,
                plate: np.ndarray | None = None) -> np.ndarray:
    x0, y0, x1, y1 = bbox
    h, w = frame.shape[:2]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(w, x1), min(h, y1)
    if x1 <= x0 or y1 <= y0:
        return np.zeros((0, 0), dtype=bool)
    crop = frame[y0:y1, x0:x1]
    if plate is not None:
        return foreground_mask_plate(crop, plate[y0:y1, x0:x1], thr)
    return foreground_mask(crop, bg_rgb, thr)


def _glyph_core_mask(crop: np.ndarray, mask: np.ndarray, bg_rgb: tuple,
                     plate: np.ndarray | None = None) -> np.ndarray:
    """Exclude glow and plate differences from colour/opacity measurements."""
    if not mask.any():
        return mask
    pixels_lab = rgb_to_lab(crop[mask][None])[0]
    background_lab = (rgb_to_lab(plate[mask][None])[0] if plate is not None else
                      rgb_to_lab(np.array(bg_rgb, np.uint8).reshape(1, 1, 3))[0, 0])
    contrast = np.linalg.norm(pixels_lab - background_lab, axis=1)
    # ponytail: a single fill from the strongest contrast decile; multicolour text needs glyph segmentation.
    core = np.zeros_like(mask)
    core[mask] = contrast >= np.percentile(contrast, 90)
    return core


def _core_pixels(frame: np.ndarray, bbox, bg_rgb: tuple, plate: np.ndarray | None) -> tuple[np.ndarray, np.ndarray]:
    """(glyph core pixels, the plate behind them), uint8 N×3, the box clipped to the frame."""
    x0, y0, x1, y1 = bbox
    fh, fw = frame.shape[:2]
    cx0, cy0, cx1, cy1 = max(0, x0), max(0, y0), min(fw, x1), min(fh, y1)
    crop = frame[cy0:cy1, cx0:cx1]
    local_plate = plate[cy0:cy1, cx0:cx1] if plate is not None else None
    core = _glyph_core_mask(crop, stroke_mask(frame, bbox, bg_rgb, plate=plate), bg_rgb, local_plate)
    px = crop[core] if core.any() else np.zeros((0, 3), np.uint8)
    behind = local_plate[core] if local_plate is not None and core.any() else np.broadcast_to(np.asarray(bg_rgb, np.uint8), px.shape)
    return px, behind


def _box_area(box: TextBox) -> int:
    return (box.bbox[2] - box.bbox[0]) * (box.bbox[3] - box.bbox[1])


def _fade_fit(px: np.ndarray, behind: np.ndarray, colour: np.ndarray) -> tuple[float, float]:
    """(level, residual) of glyph pixels as `colour` faded over the plate behind them: the median projection of
    px − B on colour − B (not clipped: a more opaque frame is above 1), and the median ΔE76 between px and
    B + level·(colour − B) per pixel (the distance off that line: another colour, clutter)."""
    p, b = px.astype(np.float32), behind.astype(np.float32)
    d = np.asarray(colour, np.float32) - b
    n = np.sum(d * d, axis=1)
    ok = n > 1e-6
    if not ok.any():
        return 0.0, float("inf")
    a = np.sum((p - b) * d, axis=1)[ok] / n[ok]
    fit = np.clip(b[ok] + a[:, None] * d[ok], 0, 255)
    return float(np.median(a)), float(np.median(delta_e(srgb8_to_lab(p[ok].astype(np.uint8)), srgb8_to_lab(np.rint(fit).astype(np.uint8)))))


def full_opacity_frame(track: TextTrack, frames: np.ndarray, bg_rgb: tuple, cf: int, plate: np.ndarray | None = None) -> int:
    """The canonical frame of a text that fades: `cf` (the largest box) when its glyphs are at full opacity, else the
    largest box among the frames, inside the picture, that are. The text's colour is the median of the frames' glyph
    core colours; a frame shows it (faded or not) when its core lies on the line from the plate to that colour
    (`_fade_fit` residual ≤ ΔE 8); the peak is the level 3 such frames reach, and full opacity ≥ 0.97 of it. When
    fewer than half the frames show that colour (clutter, a colour animation) `cf` stays. A faded-in title's largest
    OCR box is often a faint frame, whose colour would define opacity 1."""
    H, W = frames.shape[1:3]
    fit = {}
    inside = []
    for f, b in track.boxes.items():
        px, behind = _core_pixels(frames[f], b.bbox, bg_rgb, plate)
        if not len(px):
            continue
        if len(px) > PEAK_PX:
            keep = np.linspace(0, len(px) - 1, PEAK_PX).astype(int)
            px, behind = px[keep], behind[keep]
        fit[f] = (px, behind)
        x0, y0, x1, y1 = b.bbox
        if x0 >= 0 and y0 >= 0 and x1 <= W and y1 <= H:
            inside.append(f)
    if len(inside) < PEAK_SUPPORT:
        return cf
    colour = np.median(np.stack([np.median(fit[f][0], axis=0) for f in inside]), axis=0)
    fit = {f: _fade_fit(px, behind, colour) for f, (px, behind) in fit.items()}
    line = [f for f in inside if fit[f][1] <= FIT_DE]
    if 2 * len(line) < len(inside):
        return cf
    peak = sorted((fit[f][0] for f in line), reverse=True)[min(PEAK_SUPPORT, len(line)) - 1]
    if peak <= 0:
        return cf
    if cf in fit and fit[cf][1] <= FIT_DE and fit[cf][0] >= FULL_OPACITY * peak:
        return cf
    return max((f for f in line if fit[f][0] >= FULL_OPACITY * peak), key=lambda f: _box_area(track.boxes[f]))


def text_props(track: TextTrack, frames: np.ndarray, bg_rgb: tuple, n_frames: int, first_frame: int, *,
               infer_font: bool = True, plate: np.ndarray | None = None, full_opacity: bool = True,
               notes: list[str] | None = None):
    """`full_opacity`: a text that fades takes its canonical frame (and so its colour and opacity 1) at full opacity
    (`full_opacity_frame`; a reveal keeps its widest box). `notes` collects report messages."""
    reveal = getattr(track, "reveal", False)
    if reveal:
        cf = _widest_box(track).frame
    else:
        cf = max(track.boxes, key=lambda f: _box_area(track.boxes[f]))
        if full_opacity and FULL_OPACITY > 0:
            try:
                largest, cf = cf, full_opacity_frame(track, frames, bg_rgb, cf, plate)
                if cf != largest:
                    log.info("text track %s: canonical frame %s (full opacity), not %s (largest box, faded)", track.id, cf, largest)
            except Exception as e:   # fail soft: today's canonical frame (the largest box)
                log.exception("full-opacity frame failed for text track %s", track.id)
                if notes is not None:
                    notes.append(f"full-opacity frame not found ({type(e).__name__}: {e}); kept the largest box"[:200])
    cb = track.boxes[cf].bbox
    frame_h, frame_w = frames[cf].shape[:2]
    cx0, cy0, cx1, cy1 = max(0, cb[0]), max(0, cb[1]), min(frame_w, cb[2]), min(frame_h, cb[3])
    crop = frames[cf][cy0:cy1, cx0:cx1]
    sm = stroke_mask(frames[cf], cb, bg_rgb, plate=plate)
    canon = np.dstack([crop, (sm * 255).astype(np.uint8)])
    cw, ch = cb[2] - cb[0], cb[3] - cb[1]
    track_h = track.boxes[track.first].bbox[3] - track.boxes[track.first].bbox[1]
    local_plate = plate[cy0:cy1, cx0:cx1] if plate is not None else None
    core = _glyph_core_mask(crop, sm, bg_rgb, local_plate)
    stroke_px = crop[core].astype(np.float32) if core.any() else crop.reshape(-1, 3).astype(np.float32)
    # The core-mask colour is only the fallback (and the opacity reference below): the style phase (textstyle)
    # measures the fill on the matted texture against the local plate and replaces it.
    col = np.median(stroke_px, axis=0)
    color = "#%02x%02x%02x" % tuple(int(v) for v in col)
    c = col - np.array(bg_rgb, np.float32); n = float(np.dot(c, c))
    raw = np.full((n_frames, 9), np.nan)
    for f, b in track.boxes.items():
        x0, y0, x1, y1 = b.bbox
        px, bg_px = (a.astype(np.float32) for a in _core_pixels(frames[f], b.bbox, bg_rgb, plate))
        if plate is None:
            opacity = float(np.clip(np.median(((px - np.array(bg_rgb, np.float32)) @ c) / n), 0, 1)) if (n > 1e-6 and len(px)) else 1.0
        else:
            opacity = opacity_against_plate(px, bg_px, col)
        # A reveal clips the full texture; shrinking sx too would clip twice.
        x = x0 + cw / 2 if reveal else (x0 + x1) / 2
        sx = 1.0 if reveal else (x1 - x0) / cw
        fraction = float(np.clip((x1 - cb[0]) / cw, 0, 1)) if reveal else 1.0
        raw[f - first_frame] = [x, (y0 + y1) / 2, sx, (y1 - y0) / ch, 0.0, 0.0, 0.0, opacity, fraction]
    if not infer_font:
        return raw, canon, cf, None, None
    # A first guess only: the style phase matches the font on the matted texture (fonts.match, Task 10) and
    # replaces it; this one stays when that fails.
    size = float(min(ch, track_h) * 0.8)
    font = FontGuess(family_guess="sans-serif", weight=700 if sm.mean() > 0.35 else 400, size_px=size, confidence=0.5)
    return raw, canon, cf, font, color
