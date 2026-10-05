from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np


@dataclass
class Solid:
    frames: dict[int, tuple[tuple[int, int, int, int], np.ndarray]] = field(default_factory=dict)
    members: list[int] = field(default_factory=list)


def _centroid(mask):
    ys, xs = np.nonzero(mask)
    return np.array([xs.mean(), ys.mean()])


def _change(frames, solid):
    crops = {}
    sizes = []
    for f, (box, _) in solid.frames.items():
        x0, y0, x1, y1 = box
        sizes.append((x1 - x0, y1 - y0))
        crops[f] = cv2.resize(cv2.cvtColor(frames[f, y0:y1, x0:x1], cv2.COLOR_RGB2GRAY),
                             (64, 64)).astype(np.float32)
    sizes = np.array(sizes)
    stable = np.all(np.ptp(sizes, axis=0) / np.maximum(sizes.mean(axis=0), 1) < 0.10)
    changes = []
    for f, b in crops.items():
        if f - 1 not in crops:
            continue
        a = crops[f - 1]
        ac, bc = a - a.mean(), b - b.mean()
        denom = np.linalg.norm(ac) * np.linalg.norm(bc)
        if denom <= 1e-6:
            continue  # Flat crops have no normalized interior-change signal.
        ncc = float(np.sum(ac * bc) / denom)
        changes.append(1 - ncc)
    return stable and bool(changes), float(np.mean(changes)) if changes else 0.0


def find_solids(frames, fg, obj_tracks, text_masks, min_life=12, min_members=3, change_thr=0.15) -> list[Solid]:
    """Track 5px-dilated non-text foreground components and qualify interior change."""
    kernel = np.ones((11, 11), np.uint8)  # radius 5px
    candidates = []
    previous = []
    # ponytail: adjacent-frame one-to-one IoU; bridge occlusion gaps with a predictive component tracker.
    for f in range(len(frames)):
        foreground = fg[f].astype(bool) & ~text_masks[f].astype(bool)
        count, labels = cv2.connectedComponents(cv2.dilate(foreground.astype(np.uint8), kernel))
        components = [labels == i for i in range(1, count)]
        pairs = []
        for i, (_, a) in enumerate(previous):
            for j, b in enumerate(components):
                overlap = np.count_nonzero(a & b)
                iou = overlap / max(1, np.count_nonzero(a | b))
                if iou >= 0.5:
                    pairs.append((iou, i, j))
        matches, used = {}, set()
        for _, i, j in sorted(pairs, reverse=True):
            if i not in used and j not in matches:
                matches[j] = previous[i][0]
                used.add(i)
        current = []
        for j, component in enumerate(components):
            solid = matches.get(j)
            if solid is None:
                solid = Solid()
                candidates.append(solid)
            mask = component & foreground
            ys, xs = np.nonzero(mask)
            box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
            solid.frames[f] = (box, mask)
            current.append((solid, component))
        previous = current
    result = []
    for solid in candidates:
        life = len(solid.frames)
        if life < max(min_life, 0.30 * len(frames)):
            continue
        membership_masks = {f: cv2.dilate(mask.astype(np.uint8), kernel).astype(bool)
                            for f, (_, mask) in solid.frames.items()}
        members = []
        for track in obj_tracks:
            inside = 0
            for f, region in track.regions.items():
                if f in solid.frames:
                    mask = membership_masks[f]
                    x, y = (int(round(v)) for v in region.centroid)
                    inside += 0 <= y < mask.shape[0] and 0 <= x < mask.shape[1] and bool(mask[y, x])
            if inside >= len(track.regions) / 2:
                members.append(track)
        fragmented = len(members) >= min_members and np.median([len(t.regions) for t in members]) < life * 0.50
        stable, change = _change(frames, solid)
        if fragmented or (stable and change >= change_thr):
            solid.members = [t.id for t in members]
            result.append(solid)
    return result


def estimate_spin(frames, masks, boxes):
    """Yaw/pitch (deg) of a roughly convex solid from interior flow: u = ω·z on a sphere (orthographic).
    ponytail: sphere-like assumption; a learned pose tracker if real objects disagree.
    """
    n = len(frames); rx = np.zeros(n); ry = np.zeros(n)
    for f in range(1, n):
        rx[f], ry[f] = rx[f - 1], ry[f - 1]
        if masks[f - 1] is None or masks[f] is None:
            continue
        g0, g1 = (cv2.cvtColor(frames[i], cv2.COLOR_RGB2GRAY) for i in (f - 1, f))
        pts = cv2.goodFeaturesToTrack(g0, 200, 0.01, 4, mask=masks[f - 1].astype(np.uint8))
        if pts is None:
            continue
        nxt, st, _ = cv2.calcOpticalFlowPyrLK(g0, g1, pts, None)
        if nxt is None or st is None:
            continue
        p, d = pts.reshape(-1, 2)[st.ravel() == 1], (nxt - pts).reshape(-1, 2)[st.ravel() == 1]
        x0, y0, x1, y1 = boxes[f - 1]; cx, cy, R = (x0 + x1) / 2, (y0 + y1) / 2, max(1.0, (x1 - x0 + y1 - y0) / 4)
        d = d - (_centroid(masks[f]) - _centroid(masks[f - 1]))
        z = np.sqrt(np.maximum(R * R - (p[:, 0] - cx) ** 2 - (p[:, 1] - cy) ** 2, 0))
        ok = z >= 0.3 * R
        if ok.sum() >= 5:
            ry[f] += np.degrees(np.median(d[ok, 0] / z[ok]))
            rx[f] += np.degrees(np.median(d[ok, 1] / z[ok]))
    return rx, ry


def solid_props(solid, frames):
    cf = max(solid.frames, key=lambda f: np.count_nonzero(solid.frames[f][1]))
    (x0, y0, x1, y1), mask = solid.frames[cf]
    canon = np.dstack([frames[cf, y0:y1, x0:x1], mask[y0:y1, x0:x1].astype(np.uint8) * 255])
    boxes = [solid.frames[f][0] if f in solid.frames else None for f in range(len(frames))]
    masks = [solid.frames[f][1] if f in solid.frames else None for f in range(len(frames))]
    rx, ry = estimate_spin(frames, masks, boxes)
    raw = np.full((len(frames), 11), np.nan)
    for f, (box, _) in solid.frames.items():
        a, b, c, d = box
        raw[f, :8] = [(a + c) / 2, (b + d) / 2, (c - a) / (x1 - x0), (d - b) / (y1 - y0), 0, 0, 0, 1]
        raw[f, 9:] = [rx[f], ry[f]]
    return dict(raw=raw, canon=canon, cf=cf, kind="sprite", pending_asset="3d",
                first=min(solid.frames), last=max(solid.frames), boxes=boxes)
