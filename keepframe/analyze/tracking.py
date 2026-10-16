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


def _trim_tail_crumbs(track: ObjectTrack) -> None:
    """Drop shrunken tail regions after opacity/scale transitions (not whole tracks)."""
    max_a = max(r.area for r in track.regions.values())
    med_f = float(np.median(list(track.regions.keys())))
    for f in list(track.regions):
        if f > med_f and track.regions[f].area < 0.6 * max_a:
            del track.regions[f]


def _merge_adjacent_tracks(tracks: list[ObjectTrack], max_gap: int = 8, color_thr: float = 35.0,
                           max_dist: float = 80.0) -> list[ObjectTrack]:
    """Join nearby same-colour fragments across short, non-overlapping gaps."""
    if len(tracks) < 2:
        return tracks
    colors = {t.id: np.mean([r.color for r in t.regions.values()], axis=0) for t in tracks}
    merged: list[ObjectTrack] = []
    for t in sorted(tracks, key=lambda t: t.first):
        first = t.regions[t.first]
        match = None
        best_distance = math.inf
        for prev in merged:
            if not 1 <= t.first - prev.last <= max_gap:
                continue
            if float(np.linalg.norm(colors[prev.id] - colors[t.id])) > color_thr:
                continue
            last = prev.regions[prev.last]
            predicted = _predict(prev, t.first)
            dx, dy = predicted[0] - last.centroid[0], predicted[1] - last.centroid[1]
            # Occlusion can move a visible centroid across the object's width.
            gap_x = max(first.bbox[0] - last.bbox[2] - dx, last.bbox[0] + dx - first.bbox[2], 0)
            gap_y = max(first.bbox[1] - last.bbox[3] - dy, last.bbox[1] + dy - first.bbox[3], 0)
            if math.hypot(gap_x, gap_y) > max_dist:
                continue
            distance = math.dist(predicted, first.centroid)
            if distance < best_distance:
                match, best_distance = prev, distance
        if match is None:
            merged.append(t)
        else:
            colors[match.id] = (
                colors[match.id] * len(match.regions) + colors[t.id] * len(t.regions)
            ) / (len(match.regions) + len(t.regions))
            match.regions.update(t.regions)
    return merged


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
