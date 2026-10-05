from __future__ import annotations
from dataclasses import dataclass
import numpy as np
from ..ir.schema import Scene
from ..ir.tracks import element_bbox, eval_props

COLS = ("x", "y", "sx", "sy", "rot", "opacity")
EPS = {"translation": 0.5, "rotation": 0.25, "scale": 0.002, "opacity": 0.005, "reveal": 0.005, "spin": 0.25}
TYPE_ORDER = ("translation", "rotation", "scale", "opacity", "reveal", "spin")


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


def _validated_matrices(scene: Scene, matrices: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Validate source animation matrices before deriving motion signals."""
    expected = {element.id for element in scene.elements}
    if set(matrices) != expected:
        missing = sorted(expected - set(matrices), key=str)
        extra = sorted(set(matrices) - expected, key=str)
        detail = []
        if missing:
            detail.append(f"missing sources {missing}")
        if extra:
            detail.append(f"unknown sources {extra}")
        raise ValueError("motion matrices do not match scene elements: " + ", ".join(detail))
    out: dict[str, np.ndarray] = {}
    expected_shape = (scene.frames, len(COLS))
    for source_id in sorted(expected):
        matrix = matrices[source_id]
        if not isinstance(matrix, np.ndarray):
            raise ValueError(f"motion matrix for {source_id} must be a float array")
        if matrix.shape != expected_shape:
            raise ValueError(
                f"motion matrix for {source_id} has shape {matrix.shape}, expected {expected_shape}"
            )
        if not np.issubdtype(matrix.dtype, np.floating):
            raise ValueError(f"motion matrix for {source_id} must have a floating dtype")
        rows = np.isfinite(matrix).all(axis=1) | np.isnan(matrix).all(axis=1)
        if not bool(rows.all()):
            raise ValueError(f"motion matrix for {source_id} has partial NaN or non-finite rows")
        out[source_id] = matrix
    return out


def extract_motions_from_matrices(
    scene: Scene,
    matrices: dict[str, np.ndarray],
    eps: dict[str, float] | None = None,
    *,
    extra_channels: dict[str, np.ndarray] | None = None,
) -> list[Motion]:
    """Extract motions from six-column transforms and optional reveal/rx/ry.

    Extra channels are observations, never inferred from scene tracks. All
    channels share the same per-element chronological motion numbering.

    Inactive source frames are represented by rows containing only NaNs.  This
    function intentionally does not infer visibility from the scene: callers
    providing observed matrices own the active-frame mask.
    """
    matrices = _validated_matrices(scene, matrices)
    eps = {**EPS, **(eps or {})}
    motions: list[Motion] = []
    for el in scene.elements:
        m = matrices[el.id]
        d = np.diff(m, axis=0)  # d[f] = m[f+1]-m[f]
        found: list[tuple[int, str, int, tuple | None, float]] = []
        signals = {
            "translation": np.hypot(d[:, 0], d[:, 1]),
            "rotation": np.abs(d[:, 4]),
            "scale": np.abs(d[:, 2]) + np.abs(d[:, 3]),
            "opacity": np.abs(d[:, 5]),
        }
        for typ in signals:
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
            motions.append(
                Motion(
                    id=f"m_{el.id}_{n}",
                    element=el.id,
                    type=typ,
                    start=start,
                    end=end,
                    dir=direction,
                    mag=mag,
                    dur=end - start,
                )
            )
    if extra_channels is not None:
        if set(extra_channels) != {el.id for el in scene.elements}:
            raise ValueError("extra motion channels do not match scene elements")
        for el in scene.elements:
            values = extra_channels[el.id]
            if not isinstance(values, np.ndarray) or values.shape != (scene.frames, 3) or not np.issubdtype(values.dtype, np.floating):
                raise ValueError(f"extra motion channels for {el.id} must be a floating reveal/rx/ry array")
            if not bool((np.isfinite(values).all(axis=1) | np.isnan(values).all(axis=1)).all()):
                raise ValueError(f"extra motion channels for {el.id} have invalid rows")
            delta = np.diff(values, axis=0)
            channels = [("reveal", 0)]
            if el.kind == "3d":
                channels.extend([("spin", 1), ("spin", 2)])
            for typ, channel in channels:
                signal = np.abs(delta[:, channel])
                for a, b in _runs(np.nan_to_num(signal, nan=0.0) > eps[typ]):
                    start, end = int(a), int(b) + 1
                    mag = float(values[end, channel] - values[start, channel])
                    motions.append(Motion("", el.id, typ, start, end, None, mag, end - start))
    ordered = []
    for el in scene.elements:
        found = sorted((m for m in motions if m.element == el.id), key=lambda m: (m.start, TYPE_ORDER.index(m.type)))
        for n, motion in enumerate(found, 1):
            motion.id = f"m_{el.id}_{n}"
        ordered.extend(found)
    return ordered


def extract_motions(scene: Scene, eps: dict[str, float] | None = None) -> list[Motion]:
    extra = {}
    for el in scene.elements:
        values = np.full((scene.frames, 3), np.nan)
        for f in range(max(0, el.visible[0]), min(el.visible[1], scene.frames - 1) + 1):
            props = eval_props(el, f)
            values[f] = [props["reveal"], props["rx"], props["ry"]]
        extra[el.id] = values
    return extract_motions_from_matrices(scene, animation_matrix(scene), eps=eps, extra_channels=extra)
