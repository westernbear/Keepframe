from __future__ import annotations
from typing import Protocol
import numpy as np
from ..ir.schema import Element, Group
from ..ir.tracks import element_bbox

# ponytail: heuristic roles and motion-correlation groups; VLM captions are opt-in through Captioner and never touch timing.


class Captioner(Protocol):
    def caption(self, texture: np.ndarray, text: str | None) -> str: ...


class NullCaptioner:
    def caption(self, texture: np.ndarray, text: str | None) -> str:
        return ""


def assign_roles(elements: list[Element]) -> None:
    non_text = [e for e in elements if e.kind != "text"]
    primary = max(non_text, key=lambda e: e.canonical.width * e.canonical.height * (e.visible[1] - e.visible[0] + 1), default=None)
    for e in elements:
        e.role = "text" if e.kind == "text" else ("primary" if e is primary else "secondary")


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    da, db = np.diff(a, axis=0).ravel(), np.diff(b, axis=0).ravel()
    if np.linalg.norm(da) < 1e-6 or np.linalg.norm(db) < 1e-6:
        return 1.0 if np.linalg.norm(da) < 1e-6 and np.linalg.norm(db) < 1e-6 else 0.0
    return float(np.dot(da, db) / (np.linalg.norm(da) * np.linalg.norm(db)))


def group_by_motion(elements: list[Element], raws: dict[str, np.ndarray], corr_thr: float = 0.98, start_tol: int = 1) -> list[Group]:
    parent = {e.id: e.id for e in elements}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x

    for i, a in enumerate(elements):
        for b in elements[i + 1:]:
            if abs(a.visible[0] - b.visible[0]) > start_tol:
                continue
            lo, hi = max(a.visible[0], b.visible[0]), min(a.visible[1], b.visible[1])
            if hi - lo < 2:
                continue
            ra, rb = raws[a.id][lo:hi + 1, :2], raws[b.id][lo:hi + 1, :2]
            ok = ~(np.isnan(ra[:, 0]) | np.isnan(rb[:, 0]))
            if ok.sum() >= 3 and _corr(ra[ok], rb[ok]) >= corr_thr:
                parent[find(a.id)] = find(b.id)
    buckets: dict[str, list[str]] = {}
    for e in elements:
        buckets.setdefault(find(e.id), []).append(e.id)
    groups = [sorted(m) for m in buckets.values() if len(m) >= 2]
    return [Group(id=f"g{i}", members=m, reason="moves together") for i, m in enumerate(sorted(groups), 1)]


def _iou(a, b) -> float:
    ix, iy = max(0.0, min(a[2], b[2]) - max(a[0], b[0])), max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - ix * iy
    return ix * iy / union if union > 0 else 0.0


def group_successions(elements: list[Element], gap: int = 2, iou_thr: float = 0.5) -> list[Group]:
    """Elements that take over where another left off, the same place one after another (a counter's values, the
    pieces of a pop or a stagger the analysis split in time): B starts 0..gap frames after A's last frame and A's
    last box overlaps B's first at IoU ≥ iou_thr. An observation for the readers, not one object: renders and
    tracks are untouched."""
    parent = {e.id: e.id for e in elements}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x

    ends = {e.id: element_bbox(e, e.visible[1]) for e in elements}
    for a in elements:
        for b in elements:
            if b is not a and 0 <= b.visible[0] - a.visible[1] <= gap and _iou(ends[a.id], element_bbox(b, b.visible[0])) >= iou_thr:
                parent[find(a.id)] = find(b.id)
    start = {e.id: e.visible[0] for e in elements}
    buckets: dict[str, list[str]] = {}
    for e in elements:
        buckets.setdefault(find(e.id), []).append(e.id)
    groups = sorted(sorted(m, key=lambda i: (start[i], i)) for m in buckets.values() if len(m) >= 2)
    return [Group(id=f"seq{i}", members=m, reason="same place, one after another") for i, m in enumerate(groups, 1)]
