"""The tracker's learned same-content judgement (fixes7): P(two regions in two frames hold the same content), from 18
pair features and a small MLP trained offline (scripts/train_assoc.py, torch) and run here in numpy.

A region's descriptor (colour, colour histogram, a 24 px grey crop and its gradient) is computed once per region; a
pair's features are cheap differences of two descriptors and their geometry, plus its context: how much nearer each
region's nearest neighbour in the other frame is (a far identical shape is a fast mover when nothing nearer could
continue it, another copy when its own continuation is nearer). `load_matcher()` returns None when the weights are
missing or do not fit, and the tracker keeps its rules."""
from __future__ import annotations
import json
import math
from functools import lru_cache
from pathlib import Path
import cv2
import numpy as np
from ..ir.colour import delta_e, srgb8_to_lab
from .regions import Region

WEIGHTS = Path(__file__).with_name("assoc.npz")
CROP = 24
N_FEATURES = 18
MAX_GAP = 8          # _merge_adjacent_tracks' longest gap: the pairs the matcher is trained on
P_SAME = 0.5


def descriptor(r: Region, frame: np.ndarray) -> dict:
    """What a pair's appearance features need from one region (its frame is the region's own)."""
    x0, y0, x1, y1 = r.bbox
    crop = frame[y0:y1, x0:x1]
    m = r.mask.astype(bool)
    grey = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY).astype(np.float32)
    grey = np.where(m, grey, grey[m].mean() if m.any() else 0.0)   # outside the mask: the region's mean
    small = cv2.resize(grey, (CROP, CROP), interpolation=cv2.INTER_AREA)
    gx, gy = cv2.Sobel(small, cv2.CV_32F, 1, 0), cv2.Sobel(small, cv2.CV_32F, 0, 1)
    q = (crop[m] // 64).astype(np.int32) if m.any() else np.zeros((1, 3), np.int32)
    hist = np.bincount(q[:, 0] * 16 + q[:, 1] * 4 + q[:, 2], minlength=64).astype(np.float32)
    H, W = frame.shape[:2]
    return {"grey": small - small.mean(), "grad": np.hypot(gx, gy) - np.hypot(gx, gy).mean(), "hist": hist / max(hist.sum(), 1.0),
            "lab": srgb8_to_lab(np.clip(np.round(np.asarray(r.color)), 0, 255).astype(np.uint8)),
            "edge": float(x0 <= 0 or y0 <= 0 or x1 >= W or y1 >= H), "diag": math.hypot(W, H)}


def _ncc(a: np.ndarray, b: np.ndarray) -> float:
    d = math.sqrt(float((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / d) if d > 1e-6 else 0.0


def _aligned_iou(a: Region, b: Region, scale: bool) -> float:
    """IoU of the two masks with their centroids on one point (b resized to a's area when `scale`).
    shortcut: about 90 % of a pair's 4 ms is the mask centroids found here (mk571, 482 frames at 1080x1920: tracking
    149 s against 0.5 s for the rules); take them from Region.centroid, then retrain, when tracking time matters."""
    mb = b.mask.astype(np.uint8)
    if scale:
        s = math.sqrt(max(a.area, 1) / max(b.area, 1))
        mb = cv2.resize(mb, (max(1, round(mb.shape[1] * s)), max(1, round(mb.shape[0] * s))), interpolation=cv2.INTER_NEAREST)
    ma = a.mask.astype(np.uint8)
    h, w = max(ma.shape[0], mb.shape[0]) * 2, max(ma.shape[1], mb.shape[1]) * 2
    canvas_a, canvas_b = np.zeros((h, w), np.uint8), np.zeros((h, w), np.uint8)
    for canvas, m in ((canvas_a, ma), (canvas_b, mb)):
        ys, xs = np.nonzero(m)
        if len(xs) == 0:
            return 0.0
        oy, ox = h // 2 - int(round(ys.mean())), w // 2 - int(round(xs.mean()))
        canvas[oy:oy + m.shape[0], ox:ox + m.shape[1]] |= m
    union = int((canvas_a | canvas_b).sum())
    return float((canvas_a & canvas_b).sum() / union) if union else 0.0


def _bbox_overlap(a, b) -> tuple[float, float, float]:
    """bbox IoU, and the share of each box inside the other."""
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    area = lambda r: max(1, (r[2] - r[0]) * (r[3] - r[1]))
    return inter / (area(a) + area(b) - inter), inter / area(a), inter / area(b)


def nearest(r: Region, others: list[Region]) -> float:
    """Distance from r's centroid to the nearest centroid among `others` (inf when there are none)."""
    if not others:
        return math.inf
    c = np.array([o.centroid for o in others], np.float64)
    return float(np.hypot(c[:, 0] - r.centroid[0], c[:, 1] - r.centroid[1]).min())


def pair_features(a: Region, da: dict, b: Region, db: dict, gap: int, near: tuple[float, float]) -> np.ndarray:
    """18 numbers for (a, b), a seen `gap` frames before b; `near`: a's nearest distance to b's frame's regions and
    b's to a's frame's (`nearest`)."""
    size = math.sqrt((a.area + b.area) / 2)
    d = math.hypot(b.centroid[0] - a.centroid[0], b.centroid[1] - a.centroid[1])
    iou, a_in, b_in = _bbox_overlap(a.bbox, b.bbox)
    wa, ha = max(1, a.bbox[2] - a.bbox[0]), max(1, a.bbox[3] - a.bbox[1])
    wb, hb = max(1, b.bbox[2] - b.bbox[0]), max(1, b.bbox[3] - b.bbox[1])
    return np.array([
        math.log1p(d / max(size, 1.0)),
        math.log(max(b.area, 1) / max(a.area, 1)),
        iou, a_in, b_in,
        _aligned_iou(a, b, False), _aligned_iou(a, b, True),
        math.log((wb / hb) / (wa / ha)),
        gap / MAX_GAP,
        da["edge"], db["edge"],
        float(delta_e(da["lab"], db["lab"])) / 50.0,
        float(cv2.compareHist(da["hist"], db["hist"], cv2.HISTCMP_BHATTACHARYYA)),
        _ncc(da["grey"], db["grey"]), _ncc(da["grad"], db["grad"]),
        math.log(max(size, 1.0) / da["diag"]),
        math.log((d + 1) / (min(near[0], d) + 1)), math.log((d + 1) / (min(near[1], d) + 1)),
    ], np.float32)


class Matcher:
    """An 18 -> h1 -> h2 -> 1 MLP with ReLU and a sigmoid, on standardised features."""

    def __init__(self, w: dict):
        self.mean, self.std = w["mean"].astype(np.float32), np.maximum(w["std"].astype(np.float32), 1e-6)
        self.layers = [(w["W1"], w["b1"]), (w["W2"], w["b2"]), (w["W3"], w["b3"])]
        self.meta = json.loads(str(w["meta"])) if "meta" in w else {}
        if self.mean.shape != (N_FEATURES,) or self.layers[0][0].shape[0] != N_FEATURES or self.layers[-1][0].shape[1] != 1:
            raise ValueError("assoc weights do not fit the features")

    def prob(self, x: np.ndarray) -> np.ndarray:
        h = (np.atleast_2d(x).astype(np.float32) - self.mean) / self.std
        for i, (W, b) in enumerate(self.layers):
            h = h @ W + b
            if i < len(self.layers) - 1:
                h = np.maximum(h, 0.0)
        return (1.0 / (1.0 + np.exp(-h)))[:, 0]


@lru_cache(maxsize=4)
def load_matcher(path: str | None = None) -> Matcher | None:
    """The shipped matcher, or None (missing or unfit weights: the tracker keeps its rules)."""
    p = Path(path) if path else WEIGHTS
    try:
        with np.load(p, allow_pickle=False) as w:
            return Matcher({k: w[k] for k in w.files})
    except (OSError, KeyError, ValueError):
        return None
