from __future__ import annotations
from typing import Protocol
import numpy as np
from ..ir.schema import Element, Group

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
