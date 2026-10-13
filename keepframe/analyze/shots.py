from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import cv2
import numpy as np

from ..ir.colour import delta_e, srgb8_to_lab

DEFAULT_THRESHOLD = 0.42
MIN_SCENE_FRAMES = 6


@dataclass(frozen=True)
class Boundary:
    frame: int
    score: float
    start: int
    end: int
    transition: str
    flow_consistency: float = 0.0

    def to_json(self) -> dict:
        return {
            "frame": self.frame,
            "score": round(self.score, 6),
            "span": [self.start, self.end],
            "transition": self.transition,
            "flow_consistency": round(self.flow_consistency, 6),
        }


def _small(frame: np.ndarray) -> np.ndarray:
    height, width = frame.shape[:2]
    if width <= 320:
        return np.ascontiguousarray(frame)
    return cv2.resize(frame, (320, max(1, round(height * 320 / width))), interpolation=cv2.INTER_AREA)


def _gray_ssim(left: np.ndarray, right: np.ndarray) -> float:
    left = cv2.cvtColor(left, cv2.COLOR_RGB2GRAY).astype(np.float64)
    right = cv2.cvtColor(right, cv2.COLOR_RGB2GRAY).astype(np.float64)
    mu_left, mu_right = left.mean(), right.mean()
    var_left, var_right = left.var(), right.var()
    covariance = ((left - mu_left) * (right - mu_right)).mean()
    c1, c2 = 6.5025, 58.5225
    denominator = (mu_left * mu_left + mu_right * mu_right + c1) * (var_left + var_right + c2)
    if denominator == 0:
        return 1.0
    return float(np.clip(((2 * mu_left * mu_right + c1) * (2 * covariance + c2)) / denominator, -1, 1))


def _hist_distance(left: np.ndarray, right: np.ndarray) -> float:
    def hist(frame: np.ndarray) -> np.ndarray:
        hsv = cv2.cvtColor(frame, cv2.COLOR_RGB2HSV)
        value = cv2.calcHist([hsv], [0, 1], None, [32, 32], [0, 180, 0, 256])
        return cv2.normalize(value, value).flatten()

    return float(cv2.compareHist(hist(left), hist(right), cv2.HISTCMP_BHATTACHARYYA))


def frame_change_scores(frames: np.ndarray) -> list[float]:
    if len(frames) < 2:
        return []
    scaled = [_small(frame) for frame in frames]
    return [
        0.6 * _hist_distance(left, right) + 0.4 * (1 - _gray_ssim(left, right)) / 2
        for left, right in zip(scaled, scaled[1:])
    ]


SPIKE, SPIKE_WINDOW = 3.0, 2        # PySceneDetect AdaptiveDetector's defaults: a cut scores 3x the mean of its ±2 neighbours
FLOOR = 0.15 * DEFAULT_THRESHOLD    # below this a frame-to-frame change is motion, not a cut
DUPLICATE = 0.005                   # a repeated frame (30 fps content in a 60 fps file) is no neighbour
CONTENT_DE = 12                     # a pixel this far from the border colour is content (stroke_mask's distance)
GROUND_DE = 20.0                    # a new ground differs by this much (titles.COLOUR_DE: a different colour)
KEPT = 0.5                          # structure correlation above which a picture's content is still there in the next
FLASH_FRAMES = 30                   # two cuts at most this far apart that return to the first ground are a flash


def _ground(frame: np.ndarray) -> np.ndarray:
    small = _small(frame)
    return np.median(np.concatenate([small[0], small[-1], small[:, 0], small[:, -1]]), axis=0)


def _new_ground(a: np.ndarray, b: np.ndarray) -> bool:
    """The ground's colour changes by a visibly different colour (one step of a fade does not)."""
    lab = lambda f: srgb8_to_lab(np.round(_ground(f)).astype(np.uint8))
    return float(delta_e(lab(a), lab(b))) >= GROUND_DE


def _content(frame: np.ndarray) -> np.ndarray | None:
    """The frame cropped to what differs from its border colour, as a 64x64 thumbnail (None: an empty ground).
    shortcut: the border colour stands for the ground; on a strong gradient the crop takes the gradient in too."""
    small = _small(frame)
    mask = np.abs(small.astype(np.int16) - _ground(frame).astype(np.int16)).max(axis=2) > CONTENT_DE
    ys, xs = np.nonzero(mask)
    if len(xs) < 16:
        return None
    return cv2.resize(np.ascontiguousarray(small[ys.min():ys.max() + 1, xs.min():xs.max() + 1]), (64, 64), interpolation=cv2.INTER_AREA)


def _structure(frame: np.ndarray) -> np.ndarray:
    g = cv2.cvtColor(_small(frame), cv2.COLOR_RGB2GRAY).astype(np.float32)
    return cv2.GaussianBlur(np.abs(g - cv2.GaussianBlur(g, (0, 0), 2.0)), (0, 0), 1.0)


SCALES = 2.0 ** np.linspace(-1.5, 1.5, 13)   # a zoom step between 0.35x and 2.8x keeps its content


def _kept(a: np.ndarray, b: np.ndarray) -> float:
    """How much of a's structure is still in b, best over a few zooms of b about the centre and its global shift: the
    correlation of the two structure maps over a's own structure (an added word or a zoom step keeps it, a replaced
    picture does not)."""
    ha, hb0 = _structure(a), _structure(b)
    m = ha > max(4.0, np.percentile(ha, 90))
    if m.sum() < 16:
        return 1.0
    h, w = ha.shape
    best = -1.0
    for z in SCALES:
        hb = cv2.warpAffine(hb0, cv2.getRotationMatrix2D((w / 2, h / 2), 0, 1 / z), (w, h), borderMode=cv2.BORDER_CONSTANT)
        (dx, dy), _ = cv2.phaseCorrelate(ha, hb)
        hb = cv2.warpAffine(hb, np.float32([[1, 0, -dx], [0, 1, -dy]]), (w, h), borderMode=cv2.BORDER_CONSTANT)
        x, y = ha[m] - ha[m].mean(), hb[m] - hb[m].mean()
        best = max(best, float((x * y).sum() / max(np.sqrt((x * x).sum() * (y * y).sum()), 1e-6)))
    return best


def _kept_pixels(a: np.ndarray, b: np.ndarray) -> float | None:
    """The share of a's content pixels (off its ground) that b leaves as they were, in colour and in structure: a reveal
    over an empty ground changes only where a had nothing. None when a has no content."""
    sa, sb = _small(a).astype(np.int16), _small(b).astype(np.int16)
    content = np.abs(sa - _ground(a).astype(np.int16)).max(axis=2) > CONTENT_DE
    if content.sum() < 16:
        return None
    same = (np.abs(sa - sb).max(axis=2) <= 2 * CONTENT_DE) & (np.abs(_structure(a) - _structure(b)) <= 4.0)
    return float((same & content).sum() / content.sum())


def _same_picture(a: np.ndarray, b: np.ndarray) -> bool:
    """The same ground and content up to position and scale: a zoom step, or a reveal of the same picture."""
    if _new_ground(a, b):
        return False
    ca, cb = _content(a), _content(b)
    if ca is None or cb is None:
        return ca is None and cb is None
    return frame_change_scores(np.stack([ca, cb]))[0] < FLOOR


def _is_cut(a: np.ndarray, b: np.ndarray) -> bool:
    """a's picture is gone in b: not the same picture moved or zoomed, and most of a's content pixels changed (a reveal
    over an empty ground keeps them); then a new ground, or new content where neither frame's content is still there
    in the other, moved or zoomed (an entrance, an exit or an added word keeps one side's). A contentless a cuts on a new
    ground."""
    if _same_picture(a, b):
        return False
    kept = _kept_pixels(a, b)
    if kept is None:
        return _new_ground(a, b)
    if kept >= KEPT:
        return False
    if _new_ground(a, b):
        return True
    if _content(b) is None:   # going out to an empty ground
        return False
    return _kept(a, b) < KEPT and _kept(b, a) < KEPT


def detect_boundaries(frames: np.ndarray, threshold: float = DEFAULT_THRESHOLD) -> list[Boundary]:
    """Hard cuts: a change score that stands out against its neighbours (a spike, not motion; repeated frames are no
    neighbours) between two pictures that `_is_cut`; a cut and its return within FLASH_FRAMES to the first ground is a
    flash and splits nothing. Gradual transitions (wipes, dissolves) are not detected here."""
    scores = frame_change_scores(frames)
    moving = [j for j, s in enumerate(scores) if s > DUPLICATE]
    cuts = []
    for k, i in enumerate(moving):   # scores[i] compares frames i and i + 1: a cut starts frame i + 1
        near = [scores[j] for j in moving[max(0, k - SPIKE_WINDOW):k] + moving[k + 1:k + 1 + SPIKE_WINDOW]]
        if scores[i] >= max(FLOOR, SPIKE * float(np.mean(near)) if near else 0.0) and _is_cut(frames[i], frames[i + 1]):
            cuts.append(i + 1)
    flash = set()
    for a, b in zip(cuts, cuts[1:]):
        if b - a <= FLASH_FRAMES and not _new_ground(frames[a - 1], frames[b]) and _new_ground(frames[a - 1], frames[a]):
            flash |= {a, b}
    return [Boundary(f, scores[f - 1], f, f, "cut") for f in cuts if f not in flash]


def auto_scenes(frames: np.ndarray, start: int = 0) -> tuple[list[dict], list[dict]]:
    """Scenes split at the detected cuts, for an analysis given none: a cut that would leave a scene shorter than
    MIN_SCENE_FRAMES (at either end, or after the previous cut) is dropped, so nothing needs acknowledging."""
    n = len(frames)
    cuts, prev = [], 0
    for b in detect_boundaries(frames):
        if b.frame - prev >= MIN_SCENE_FRAMES and n - b.frame >= MIN_SCENE_FRAMES:
            cuts.append(b); prev = b.frame
    edges = [0, *(b.frame for b in cuts), n]
    scenes = [{"id": f"s{i}", "frames": [start + a, start + z - 1]} for i, (a, z) in enumerate(zip(edges, edges[1:]), 1)]
    transitions = [{"from": scenes[i]["id"], "to": scenes[i + 1]["id"], **b.to_json()} for i, b in enumerate(cuts)]
    return scenes, transitions


def boundary_digest(scenes: list[dict]) -> str:
    normalized = [
        {"id": str(scene["id"]), "frames": [int(scene["frames"][0]), int(scene["frames"][1])]}
        for scene in scenes
    ]
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_scenes(scenes: list[dict], start: int, end: int) -> list[dict]:
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("at least one scene is required")
    normalized: list[dict] = []
    expected = start
    ids: set[str] = set()
    for index, raw in enumerate(scenes, 1):
        scene_id = str(raw.get("id") or f"s{index}")
        frames = raw.get("frames")
        if not isinstance(frames, (list, tuple)) or len(frames) != 2:
            raise ValueError("scene frames must contain start and end")
        first, last = int(frames[0]), int(frames[1])
        if scene_id in ids or first != expected or first > last or last > end:
            raise ValueError("scenes must be unique, ordered, and contiguous")
        ids.add(scene_id)
        normalized.append({"id": scene_id, "frames": [first, last]})
        expected = last + 1
    if expected != end + 1:
        raise ValueError("scenes must cover the selected frame range")
    return normalized
