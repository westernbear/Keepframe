from __future__ import annotations
import math
from dataclasses import dataclass, field
import numpy as np
from scipy.optimize import linear_sum_assignment
from .regions import Region

# ponytail: one-to-one matching only (no split/merge): a region that two others merged into scores low against
# either with the matcher and starts a track. Upgrade path: Motico's eight mapping types with differentiable compositing.


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
                           max_dist: float = 80.0, frames: np.ndarray | None = None, matcher=None,
                           regions_by_frame: list[list[Region]] | None = None) -> list[ObjectTrack]:
    """Join nearby same-colour fragments across short, non-overlapping gaps. With a matcher (and the frames), a join
    needs P >= 0.5 that the other's first region continues the one track (`assoc.track_features`), the most likely wins
    (`regions_by_frame`: its context, the tracks' own regions when None)."""
    if len(tracks) < 2:
        return tracks
    if matcher is not None and frames is not None:
        return _merge_learned(tracks, max_gap, frames, matcher, regions_by_frame)
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


class _Descriptors(dict):
    """Region descriptors for the matcher, computed once per region, and each region's nearest distance to a frame's
    regions (`regions_by_frame`: the frames' regions, the matcher's context)."""

    def __init__(self, frames: np.ndarray, first_frame: int = 0, regions_by_frame: list[list[Region]] | None = None):
        super().__init__()
        self.frames, self.first_frame, self.rbf, self.nn, self.cov = frames, first_frame, regions_by_frame or [], {}, {}

    def near(self, r: Region, f: int) -> float:
        key = (r.frame, r.bbox, r.area, f)
        if key not in self.nn:
            from .assoc import nearest
            self.nn[key] = nearest(r, self.regions(f))
        return self.nn[key]

    def regions(self, f: int) -> list[Region]:
        i = f - self.first_frame
        return self.rbf[i] if 0 <= i < len(self.rbf) else []

    def coverage(self, f: int) -> np.ndarray:
        """assoc.coverage of frame f's regions, for the last few frames asked (candidates are at most MAX_GAP apart)."""
        if f not in self.cov:
            from .assoc import coverage
            if len(self.cov) >= 12:
                self.cov.pop(next(iter(self.cov)))
            self.cov[f] = coverage(self.regions(f), self.frames.shape[1:3])
        return self.cov[f]

    def of(self, r: Region) -> dict:
        key = (r.frame, r.bbox, r.area)
        if key not in self:
            from .assoc import descriptor
            self[key] = descriptor(r, self.frames[r.frame - self.first_frame])
        return self[key]


def _anchor(t: ObjectTrack, f: int) -> Region:
    """t's oldest region within MAX_GAP frames of f (its last when none is): what a candidate at f is also judged
    against, so that change each single step hides adds up (a glow taking in a little more of the letters each frame)."""
    from .assoc import MAX_GAP
    return t.regions[min((k for k in t.regions if f - k <= MAX_GAP), default=t.last)]


def _probs(matcher, desc: _Descriptors, cands: list[tuple[Region, Region, Region]]) -> np.ndarray:
    """P(b continues the track) for each (track's last region, its anchor, candidate b)."""
    from .assoc import track_features
    if not cands:
        return np.zeros(0, np.float32)
    return matcher.prob(np.stack([track_features(a, n, b, desc) for a, n, b in cands]))


def _merge_learned(tracks: list[ObjectTrack], max_gap: int, frames: np.ndarray, matcher,
                   regions_by_frame: list[list[Region]] | None) -> list[ObjectTrack]:
    from .assoc import P_SAME
    if regions_by_frame is None:   # the tracks' own regions
        regions_by_frame = [[] for _ in range(max(t.last for t in tracks) + 1)]
        for t in tracks:
            for f, r in t.regions.items():
                regions_by_frame[f].append(r)
    desc = _Descriptors(frames, 0, regions_by_frame)
    merged: list[ObjectTrack] = []
    for t in sorted(tracks, key=lambda t: t.first):
        cands = [prev for prev in merged if 1 <= t.first - prev.last <= max_gap]
        p = _probs(matcher, desc, [(prev.regions[prev.last], _anchor(prev, t.first), t.regions[t.first]) for prev in cands])
        if len(p) and p.max() >= P_SAME:
            cands[int(p.argmax())].regions.update(t.regions)
        else:
            merged.append(t)
    return merged


GATE = 3.0   # with the matcher, pairs further apart than GATE x max_dist (or 2 region sizes) are not scored: speed only


def track_regions(regions_by_frame: list[list[Region]], first_frame: int = 0, max_dist: float = 80.0,
                  cost_thr: float = 1.2, max_gap: int = 2, frames: np.ndarray | None = None, matcher=None) -> list[ObjectTrack]:
    """One-to-one matching of each frame's regions to the live tracks. The cost is match_cost under cost_thr, or with a
    matcher (and the frames) 1 - P(the region continues the track's content: `assoc.track_features`), matched when
    P >= 0.5."""
    if matcher is not None and frames is not None:
        from .assoc import P_SAME
        desc = _Descriptors(frames, first_frame, regions_by_frame)

        def costs(active, regions, f):
            C = np.full((len(active), len(regions)), 1e6)
            pairs, where = [], []
            for i, t in enumerate(active):
                last = t.regions[t.last]
                for j, r in enumerate(regions):
                    reach = max(GATE * max_dist, 2 * math.sqrt(max(last.area, r.area)))
                    if math.hypot(r.centroid[0] - last.centroid[0], r.centroid[1] - last.centroid[1]) <= reach:
                        pairs.append((last, _anchor(t, f), r)); where.append((i, j))
            for (i, j), p in zip(where, _probs(matcher, desc, pairs)):
                C[i, j] = 1.0 - p
            return C, 1.0 - P_SAME
    else:
        def costs(active, regions, f):
            return np.array([[match_cost(t.regions[t.last], _predict(t, f), r, max_dist) for r in regions] for t in active]), cost_thr
    tracks: list[ObjectTrack] = []
    active: list[ObjectTrack] = []
    next_id = 1
    for i, regions in enumerate(regions_by_frame):
        f = first_frame + i
        active = [t for t in active if f - t.last <= max_gap]
        matched_r: set[int] = set()
        if active and regions:
            C, thr = costs(active, regions, f)
            rows, cols = linear_sum_assignment(C)
            for a, b in zip(rows, cols):
                if C[a, b] <= thr:
                    active[a].regions[f] = regions[b]; matched_r.add(int(b))
        frame_max = max((r.area for r in regions), default=0)
        for j, r in enumerate(regions):
            if j not in matched_r:
                if frame_max > 0 and r.area * 4 < frame_max:
                    continue
                t = ObjectTrack(id=next_id, regions={f: r}); next_id += 1
                tracks.append(t); active.append(t)
    return tracks
