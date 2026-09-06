# refstudio/analyze/tracking.py
from __future__ import annotations
import math
from dataclasses import dataclass, field
import numpy as np
from scipy.optimize import linear_sum_assignment
from .regions import Region

# ponytail: one-to-one matching only (no split/merge). Upgrade path: Motico's eight mapping types with differentiable compositing.


@dataclass
class ObjectTrack:
    id: int
    regions: dict[int, Region] = field(default_factory=dict)

    @property
    def first(self) -> int:
        return min(self.regions)

    @property
    def last(self) -> int:
        return max(self.regions)


def match_cost(prev: Region, pred_centroid: tuple[float, float], cand: Region, max_dist: float) -> float:
    d = math.hypot(cand.centroid[0] - pred_centroid[0], cand.centroid[1] - pred_centroid[1])
    area = abs(math.log(max(cand.area, 1) / max(prev.area, 1)))
    col = float(np.linalg.norm(np.array(cand.color) - np.array(prev.color))) / 60.0
    return d / max_dist + area + col


def _predict(track: ObjectTrack, f: int) -> tuple[float, float]:
    fs = sorted(track.regions)
    r1 = track.regions[fs[-1]]
    if len(fs) < 2:
        return r1.centroid
    r0 = track.regions[fs[-2]]
    dt = fs[-1] - fs[-2]
    vx = (r1.centroid[0] - r0.centroid[0]) / dt
    vy = (r1.centroid[1] - r0.centroid[1]) / dt
    k = f - fs[-1]
    return (r1.centroid[0] + vx * k, r1.centroid[1] + vy * k)


def track_regions(regions_by_frame: list[list[Region]], first_frame: int = 0, max_dist: float = 80.0,
                  cost_thr: float = 1.2, max_gap: int = 2) -> list[ObjectTrack]:
    tracks: list[ObjectTrack] = []
    active: list[ObjectTrack] = []
    next_id = 1
    for i, regions in enumerate(regions_by_frame):
        f = first_frame + i
        active = [t for t in active if f - t.last <= max_gap]
        matched_r: set[int] = set()
        if active and regions:
            C = np.array([[match_cost(t.regions[t.last], _predict(t, f), r, max_dist) for r in regions] for t in active])
            rows, cols = linear_sum_assignment(C)
            for a, b in zip(rows, cols):
                if C[a, b] <= cost_thr:
                    active[a].regions[f] = regions[b]; matched_r.add(int(b))
        frame_max = max((r.area for r in regions), default=0)
        for j, r in enumerate(regions):
            if j not in matched_r:
                if frame_max > 0 and r.area * 4 < frame_max:
                    continue
                t = ObjectTrack(id=next_id, regions={f: r}); next_id += 1
                tracks.append(t); active.append(t)
    return tracks
