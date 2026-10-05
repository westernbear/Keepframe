from __future__ import annotations
import json
from pathlib import Path
import numpy as np
from ..ir.schema import Scene
from ..ir.tracks import element_bbox
from .composite import composite_scene


def reconstruction_error(scene: Scene, scene_dir: Path, frames: np.ndarray, first_frame: int, sample: int = 1) -> dict:
    rec, _ = _report_metrics(scene, scene_dir, frames, sample, confidence=False)
    return rec


def element_confidence(scene: Scene, scene_dir: Path, frames: np.ndarray, first_frame: int) -> dict[str, float]:
    _, conf = _report_metrics(scene, scene_dir, frames, None, confidence=True)
    return conf


def reconstruction_and_confidence(scene: Scene, scene_dir: Path, frames: np.ndarray, first_frame: int,
                                  sample: int = 1) -> tuple[dict, dict[str, float]]:
    """Measure both metrics with one composite per needed scene-local frame."""
    return _report_metrics(scene, scene_dir, frames, sample, confidence=True)


def _report_metrics(scene: Scene, scene_dir: Path, frames: np.ndarray, sample: int | None,
                    *, confidence: bool) -> tuple[dict, dict[str, float]]:
    reconstruction_frames = set(range(0, scene.frames, sample)) if sample is not None else set()
    sampled_elements: dict[int, list[int]] = {}
    l1s: list[list[float]] = [[] for _ in scene.elements] if confidence else []
    if confidence:
        for i, el in enumerate(scene.elements):
            step = max(1, (el.visible[1] - el.visible[0] + 1) // 6)
            for f in range(el.visible[0], el.visible[1] + 1, step):
                sampled_elements.setdefault(f, []).append(i)
    cache: dict = {}
    per = {}
    for f in sorted(reconstruction_frames | sampled_elements.keys()):
        a = composite_scene(scene, scene_dir, f, cache)
        b = frames[f].astype(np.float32) / 255.0
        delta = np.abs(a - b)
        if f in reconstruction_frames:
            per[f] = float(delta.mean())
        for i in sampled_elements.get(f, ()):
            el = scene.elements[i]
            x0, y0, x1, y1 = [int(round(v)) for v in element_bbox(el, f)]
            x0, y0 = max(0, x0), max(0, y0); x1, y1 = min(scene.size[0], x1), min(scene.size[1], y1)
            if x1 > x0 and y1 > y0:
                l1s[i].append(float(delta[y0:y1, x0:x1].mean()))
    rec = {"mean_l1": float(np.mean(list(per.values()))), "per_frame": per} if sample is not None else {}
    out = {}
    if confidence:
        for el, errors in zip(scene.elements, l1s):
            # ponytail: confidence uses bbox-local L1; perceptual scoring would replace this reduction.
            out[el.id] = float(1.0 - min(1.0, np.mean(errors) / 0.10)) if errors else 0.0
    return rec, out


def write_report(scene_dir: Path, data: dict) -> Path:
    p = Path(scene_dir) / "report.json"
    p.write_text(json.dumps(data, indent=2))
    return p
