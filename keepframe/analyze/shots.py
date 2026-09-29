from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable

import cv2
import numpy as np

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


def _groups(indices: Iterable[int], gap: int = 2) -> list[list[int]]:
    groups: list[list[int]] = []
    for index in indices:
        if not groups or index - groups[-1][-1] > gap:
            groups.append([index])
        else:
            groups[-1].append(index)
    return groups


def _monotonic_brightness(frames: list[np.ndarray]) -> bool:
    values = np.array([cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY).mean() for frame in frames])
    if len(values) < 4 or float(np.ptp(values)) < 6:
        return False
    delta = np.diff(values)
    return bool(np.all(delta >= -0.5) or np.all(delta <= 0.5))


def _flow_consistency(frames: list[np.ndarray]) -> float:
    vectors: list[np.ndarray] = []
    for left, right in zip(frames, frames[1:]):
        a = cv2.cvtColor(left, cv2.COLOR_RGB2GRAY)
        b = cv2.cvtColor(right, cv2.COLOR_RGB2GRAY)
        flow = cv2.calcOpticalFlowFarneback(a, b, None, 0.5, 3, 15, 3, 5, 1.2, 0)
        magnitude = np.linalg.norm(flow, axis=2)
        moving = magnitude > 0.5
        if moving.any():
            vectors.append(flow[moving])
    if not vectors:
        return 0.0
    vector = np.concatenate(vectors)
    unit = vector / np.maximum(np.linalg.norm(vector, axis=1, keepdims=True), 1e-6)
    return float(np.linalg.norm(unit.mean(axis=0)))


def detect_boundaries(frames: np.ndarray, threshold: float = DEFAULT_THRESHOLD) -> list[Boundary]:
    scores = frame_change_scores(frames)
    scaled = [_small(frame) for frame in frames]
    high = _groups(index + 1 for index, score in enumerate(scores) if score >= threshold)
    sustained = [
        group for group in _groups((index + 1 for index, score in enumerate(scores) if score >= threshold * 0.15), gap=1)
        if len(group) >= 3
    ]
    brightness = np.array([cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY).mean() for frame in scaled])
    fades = [
        group for group in _groups((index + 1 for index, delta in enumerate(np.abs(np.diff(brightness))) if delta >= 4), gap=1)
        if len(group) >= 3 and _monotonic_brightness(scaled[max(0, group[0] - 1):min(len(scaled), group[-1] + 2)])
    ]
    groups: list[list[int]] = []
    for candidate in sorted([*high, *sustained, *fades], key=lambda item: item[0]):
        if groups and candidate[0] - groups[-1][-1] <= 2:
            groups[-1] = sorted(set(groups[-1] + candidate))
        else:
            groups.append(candidate)
    boundaries: list[Boundary] = []
    for group in groups:
        frame = max(group, key=lambda value: scores[value - 1]) if len(group) <= 2 else group[len(group) // 2]
        start, end = group[0], group[-1]
        span = scaled[max(0, start - 1) : min(len(scaled), end + 2)]
        flow = _flow_consistency(span)
        if len(group) <= 2:
            transition = "cut"
        elif _monotonic_brightness(span):
            transition = "fade"
        elif flow >= 0.75:
            transition = "slide"
        else:
            transition = "unknown"
        boundaries.append(Boundary(frame, max(scores[value - 1] for value in group), start, end, transition, flow))
    return boundaries


def scene_layout(
    frames: np.ndarray,
    *,
    global_start: int = 0,
    threshold: float = DEFAULT_THRESHOLD,
) -> tuple[list[dict], list[dict], list[dict]]:
    boundaries = detect_boundaries(frames, threshold)
    cuts = [0, *(boundary.frame for boundary in boundaries), len(frames)]
    scenes: list[dict] = []
    warnings: list[dict] = []
    for index, (start, stop) in enumerate(zip(cuts, cuts[1:]), 1):
        scene = {"id": f"s{index}", "frames": [global_start + start, global_start + stop - 1]}
        scenes.append(scene)
        if stop - start < MIN_SCENE_FRAMES:
            warnings.append({"code": "short_scene", "scene": scene["id"], "frames": stop - start, "requires_ack": True})
    transitions = []
    for index, boundary in enumerate(boundaries):
        transitions.append(
            {
                "from": scenes[index]["id"],
                "to": scenes[index + 1]["id"],
                **boundary.to_json(),
            }
        )
    return scenes, transitions, warnings


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
