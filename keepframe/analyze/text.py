from __future__ import annotations
import difflib, math
from collections import Counter
from dataclasses import dataclass, field
from typing import Protocol
import numpy as np
from ..ir.schema import FontGuess
from ..log import get
from .background import foreground_mask
from .device import ocr_cuda, ocr_cuda_expected

log = get("keepframe.analyze")

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


class RapidOcr:
    def __init__(self):
        from rapidocr_onnxruntime import RapidOCR  # lazy import
        use_cuda = ocr_cuda()
        if use_cuda:
            log.info("ocr device=cuda")
            kw = dict(det_use_cuda=True, cls_use_cuda=True, rec_use_cuda=True)
        else:
            if ocr_cuda_expected():
                log.warning(
                    "ocr on cpu: CUDAExecutionProvider missing "
                    "(pip uninstall -y onnxruntime && pip install onnxruntime-gpu)"
                )
            else:
                log.info("ocr device=cpu")
            kw = {}
        self._ocr = RapidOCR(**kw)

    def __call__(self, frame_rgb: np.ndarray):
        result, _ = self._ocr(frame_rgb[..., ::-1])  # expects BGR
        out = []
        for quad, text, conf in result or []:
            xs = [p[0] for p in quad]; ys = [p[1] for p in quad]
            out.append((text, (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))), float(conf)))
        return out


def ocr_frames(frames: np.ndarray, ocr: Ocr, step: int = 1) -> list[list[TextBox]]:
    from keepframe.progress import report_stage
    out: list[list[TextBox]] = []
    n = len(frames)
    mark = max(1, n // 10)
    for i, f in enumerate(frames):
        if i == 0 or i + 1 == n or (i + 1) % mark == 0:
            report_stage("text", f"{i + 1}/{n}")
        out.append([TextBox(i, t, b, c) for t, b, c in ocr(f)] if i % step == 0 else [])
    return out


@dataclass
class TextTrack:
    id: int
    boxes: dict[int, TextBox] = field(default_factory=dict)
    text: str = ""

    @property
    def first(self) -> int:
        return min(self.boxes)

    @property
    def last(self) -> int:
        return max(self.boxes)


def _iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0])); iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def _centre(b) -> tuple[float, float]:
    return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)


def _sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


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
                geo = max(_iou(last.bbox, box.bbox), 1 - math.dist(_centre(last.bbox), _centre(box.bbox)) / max_dist)
                if geo < iou_thr or _sim(last.text, box.text) < 0.5:
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


def text_exclusion_mask(boxes: list[TextBox], shape: tuple[int, int], pad: int = 2) -> np.ndarray:
    m = np.zeros(shape, bool)
    for b in boxes:
        x0, y0, x1, y1 = b.bbox
        m[max(0, y0 - pad):y1 + pad, max(0, x0 - pad):x1 + pad] = True
    return m


def stroke_mask(frame: np.ndarray, bbox, bg_rgb: tuple, thr: float = 12.0) -> np.ndarray:
    x0, y0, x1, y1 = bbox
    h, w = frame.shape[:2]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(w, x1), min(h, y1)
    if x1 <= x0 or y1 <= y0:
        return np.zeros((0, 0), dtype=bool)
    return foreground_mask(frame[y0:y1, x0:x1], bg_rgb, thr)


def text_props(track: TextTrack, frames: np.ndarray, bg_rgb: tuple, n_frames: int, first_frame: int):
    cf = max(track.boxes, key=lambda f: (track.boxes[f].bbox[2] - track.boxes[f].bbox[0]) * (track.boxes[f].bbox[3] - track.boxes[f].bbox[1]))
    cb = track.boxes[cf].bbox
    frame_h, frame_w = frames[cf].shape[:2]
    cx0, cy0, cx1, cy1 = max(0, cb[0]), max(0, cb[1]), min(frame_w, cb[2]), min(frame_h, cb[3])
    crop = frames[cf][cy0:cy1, cx0:cx1]
    sm = stroke_mask(frames[cf], cb, bg_rgb)
    canon = np.dstack([crop, (sm * 255).astype(np.uint8)])
    cw, ch = cb[2] - cb[0], cb[3] - cb[1]
    track_h = track.boxes[track.first].bbox[3] - track.boxes[track.first].bbox[1]
    stroke_px = crop[sm].astype(np.float32) if sm.any() else crop.reshape(-1, 3).astype(np.float32)
    col = np.median(stroke_px, axis=0)
    color = "#%02x%02x%02x" % tuple(int(v) for v in col)
    c = col - np.array(bg_rgb, np.float32); n = float(np.dot(c, c))
    raw = np.full((n_frames, 8), np.nan)
    for f, b in track.boxes.items():
        x0, y0, x1, y1 = b.bbox
        fh, fw = frames[f].shape[:2]
        cx0, cy0, cx1, cy1 = max(0, x0), max(0, y0), min(fw, x1), min(fh, y1)
        m = stroke_mask(frames[f], b.bbox, bg_rgb)
        px = frames[f][cy0:cy1, cx0:cx1][m].astype(np.float32) if m.any() else np.zeros((0, 3), np.float32)
        opacity = float(np.clip(np.median(((px - np.array(bg_rgb, np.float32)) @ c) / n), 0, 1)) if (n > 1e-6 and len(px)) else 1.0
        raw[f - first_frame] = [(x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / cw, (y1 - y0) / ch, 0.0, 0.0, 0.0, opacity]
    font = FontGuess(family_guess="sans-serif", weight=700 if sm.mean() > 0.35 else 400, size_px=float(min(ch, track_h) * 0.8))
    return raw, canon, cf, font, color
