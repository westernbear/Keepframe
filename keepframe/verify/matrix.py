from __future__ import annotations
from dataclasses import dataclass
import numpy as np
from ..ir.schema import Scene
from ..ir.tracks import element_bbox, eval_props

COLS = ("x", "y", "sx", "sy", "rot", "opacity")
EPS = {"translation": 0.5, "rotation": 0.25, "scale": 0.002, "opacity": 0.005}
TYPE_ORDER = ("translation", "rotation", "scale", "opacity")


def animation_matrix(scene: Scene) -> dict[str, np.ndarray]:
    out = {}
    for el in scene.elements:
        m = np.full((scene.frames, len(COLS)), np.nan)
        for f in range(el.visible[0], min(el.visible[1], scene.frames - 1) + 1):
            p = eval_props(el, f)
            m[f] = [p[c] for c in COLS]
        out[el.id] = m
    return out


def bbox_matrix(scene: Scene) -> dict[str, np.ndarray]:
    out = {}
    for el in scene.elements:
        b = np.full((scene.frames, 4), np.nan)
        for f in range(el.visible[0], min(el.visible[1], scene.frames - 1) + 1):
            b[f] = element_bbox(el, f)
        out[el.id] = b
    return out


@dataclass
class Motion:
    id: str
    element: str
    type: str
    start: int
    end: int
    dir: tuple[float, float] | None
    mag: float
    dur: int


def _runs(moving: np.ndarray) -> list[tuple[int, int]]:
    """Contiguous True runs over the derivative index; bridges single-frame gaps; drops runs < 2."""
    idx = np.flatnonzero(moving)
    if idx.size == 0:
        return []
    runs, s, prev = [], idx[0], idx[0]
    for i in idx[1:]:
        if i - prev > 2:  # gap of 2+ frames ends the run (a 1-frame gap is bridged)
            runs.append((s, prev)); s = i
        prev = i
    runs.append((s, prev))
    return [(a, b) for a, b in runs if b - a + 1 >= 2]


def extract_motions(scene: Scene, eps: dict[str, float] | None = None) -> list[Motion]:
    eps = {**EPS, **(eps or {})}
    mat = animation_matrix(scene)
    motions: list[Motion] = []
    for el in scene.elements:
        m = mat[el.id]
        d = np.diff(m, axis=0)  # d[f] = m[f+1]-m[f]
        found: list[tuple[int, str, int, tuple | None, float]] = []
        signals = {
            "translation": np.hypot(d[:, 0], d[:, 1]),
            "rotation": np.abs(d[:, 4]),
            "scale": np.abs(d[:, 2]) + np.abs(d[:, 3]),
            "opacity": np.abs(d[:, 5]),
        }
        for typ in TYPE_ORDER:
            sig = np.nan_to_num(signals[typ], nan=0.0)
            for a, b in _runs(sig > eps[typ]):
                start, end = int(a), int(b) + 1
                if typ == "translation":
                    v = m[end, :2] - m[start, :2]
                    n = float(np.hypot(*v))
                    direction = (float(v[0] / n), float(v[1] / n)) if n > 0 else None
                    mag = n
                elif typ == "rotation":
                    direction, mag = None, float(m[end, 4] - m[start, 4])
                elif typ == "scale":
                    direction = None
                    mag = float(0.5 * (m[end, 2] / m[start, 2] + m[end, 3] / m[start, 3]))
                else:
                    direction, mag = None, float(m[end, 5] - m[start, 5])
                found.append((start, typ, end, direction, mag))
        found.sort(key=lambda r: (r[0], TYPE_ORDER.index(r[1])))
        for n, (start, typ, end, direction, mag) in enumerate(found, 1):
            motions.append(Motion(id=f"m_{el.id}_{n}", element=el.id, type=typ, start=start, end=end,
                                  dir=direction, mag=mag, dur=end - start))
    return motions
