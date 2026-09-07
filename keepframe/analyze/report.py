from __future__ import annotations
import json
from pathlib import Path
import numpy as np
from ..ir.schema import Scene
from ..ir.tracks import element_bbox
from .composite import composite_scene


def reconstruction_error(scene: Scene, scene_dir: Path, frames: np.ndarray, first_frame: int, sample: int = 1) -> dict:
    cache: dict = {}
    per = {}
    for f in range(0, scene.frames, sample):
        a = composite_scene(scene, scene_dir, f, cache)
        b = frames[f].astype(np.float32) / 255.0
        per[f] = float(np.abs(a - b).mean())
    return {"mean_l1": float(np.mean(list(per.values()))), "per_frame": per}


def element_confidence(scene: Scene, scene_dir: Path, frames: np.ndarray, first_frame: int) -> dict[str, float]:
    cache: dict = {}
    out = {}
    for el in scene.elements:
        fs = list(range(el.visible[0], el.visible[1] + 1, max(1, (el.visible[1] - el.visible[0] + 1) // 6)))
        l1s = []
        for f in fs:
            a = composite_scene(scene, scene_dir, f, cache)
            b = frames[f].astype(np.float32) / 255.0
            x0, y0, x1, y1 = [int(round(v)) for v in element_bbox(el, f)]
            x0, y0 = max(0, x0), max(0, y0); x1, y1 = min(scene.size[0], x1), min(scene.size[1], y1)
            if x1 > x0 and y1 > y0:
                l1s.append(float(np.abs(a[y0:y1, x0:x1] - b[y0:y1, x0:x1]).mean()))
        out[el.id] = float(1.0 - min(1.0, np.mean(l1s) / 0.10)) if l1s else 0.0   # ponytail: bbox-local L1 as confidence
    return out


def write_report(scene_dir: Path, data: dict) -> Path:
    p = Path(scene_dir) / "report.json"
    p.write_text(json.dumps(data, indent=2))
    return p
