from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from ..log import describe, get

log = get("keepframe.analyze")
WEIGHTS = Path(__file__).with_name("solids.npz")   # the learned candidate judgement (scripts/train_assoc.py solids)
N_FEATURES = 17
P_SOLID = 0.5
SOLID_JUDGE_FAILED = "solid_judge_failed"


@dataclass
class Solid:
    # Each mask is cropped to its world-space bbox; bbox[:2] is its pixel offset.
    frames: dict[int, tuple[tuple[int, int, int, int], np.ndarray]] = field(default_factory=dict)
    members: list[int] = field(default_factory=list)


def _centroid(mask):
    ys, xs = np.nonzero(mask)
    return np.array([xs.mean(), ys.mean()])


def _aligned_change(a, b, mb) -> float:
    """1 - the correlation of b with a on b's own pixels (`mb`, 0/1) after the best affine warp from identity (ECC): the
    change that one texture moved, scaled, turned or sheared cannot explain (the ground around it in the box does not
    count); the change at identity when no warp fits better, 0 on flat pixels."""
    m = mb.astype(bool)
    ac, bc = a[m] - a[m].mean(), b[m] - b[m].mean()
    denom = float(np.linalg.norm(ac) * np.linalg.norm(bc))
    if denom <= 1e-6:
        return 0.0
    change = 1.0 - float(ac @ bc) / denom
    if change < 0.02:
        return change   # nothing a warp could take out
    centre = ((a.shape[1] - 1) / 2, (a.shape[0] - 1) / 2)
    for deg in (0, 12, -12):   # shortcut: ECC converges near its start, turns up to ~20 deg a frame; log-polar phase correlation for a start if faster ones matter
        try:
            r, _ = cv2.findTransformECC(a, b, cv2.getRotationMatrix2D(centre, deg, 1.0).astype(np.float32), cv2.MOTION_AFFINE,
                                        (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 15, 1e-3), mb, 1)
        except cv2.error:
            continue
        change = min(change, 1.0 - float(r))
    return change


def _change(frames, solid, out: dict | None = None):
    """(stable box and some interior signal, mean 1 - NCC of adjacent 64 px box crops). `out`: also the judge's
    box spread, share of flat pairs, change after the best affine warp and contrast on the candidate's own pixels."""
    crops = {}
    sizes = []
    for f, (box, _) in solid.frames.items():
        x0, y0, x1, y1 = box
        sizes.append((x1 - x0, y1 - y0))
        crops[f] = cv2.resize(cv2.cvtColor(frames[f, y0:y1, x0:x1], cv2.COLOR_RGB2GRAY),
                             (64, 64)).astype(np.float32)
    sizes = np.array(sizes)
    stable = np.all(np.ptp(sizes, axis=0) / np.maximum(sizes.mean(axis=0), 1) < 0.10)
    masks = {f: cv2.resize(m.astype(np.uint8), (64, 64), interpolation=cv2.INTER_NEAREST) for f, (_, m) in solid.frames.items()} \
        if out is not None else {}
    changes, aligned, flat = [], [], 0
    for f, b in crops.items():
        if f - 1 not in crops:
            continue
        a = crops[f - 1]
        ac, bc = a - a.mean(), b - b.mean()
        denom = np.linalg.norm(ac) * np.linalg.norm(bc)
        if denom <= 1e-6:
            flat += 1
            continue  # Flat crops have no normalized interior-change signal.
        ncc = float(np.sum(ac * bc) / denom)
        changes.append(1 - ncc)
        if out is not None:
            aligned.append(_aligned_change(a, b, masks[f]))
    if out is not None:
        out.update(spread=float(np.max(np.ptp(sizes, axis=0) / np.maximum(sizes.mean(axis=0), 1))),
                   flat=flat / max(1, flat + len(changes)), aligned=float(np.mean(aligned)) if aligned else 0.0,
                   contrast=float(np.median([c[masks[f] > 0].std() if masks[f].any() else 0.0 for f, c in crops.items()])) / 64)
    return stable and bool(changes), float(np.mean(changes)) if changes else 0.0


def _features(frames, solid: Solid, members, change: float, stats: dict) -> np.ndarray:
    """N_FEATURES numbers for a candidate, the judge's input: how long it lives, how the region tracker breaks it up
    and how much of it tracks that live half its life or more cover (parts that each keep their look), how its look
    changes in its box with and without the best affine warp and its contrast (on a flat surface codec noise reads as
    change), how its outline, box and place change, its size and fill, and whether it touches the frame edge."""
    n, H, W = frames.shape[:3]
    life = len(solid.frames)
    boxes = [box for box, _ in solid.frames.values()]
    masks = [mask for _, mask in solid.frames.values()]
    lengths = np.array([len(t.regions) for t in members], float)
    areas = np.array([(x1 - x0) * (y1 - y0) for x0, y0, x1, y1 in boxes], float)
    shapes = [cv2.resize(m.astype(np.uint8), (32, 32), interpolation=cv2.INTER_NEAREST).astype(bool) for m in masks]
    outline = [1 - np.count_nonzero(p & q) / max(1, np.count_nonzero(p | q)) for p, q in zip(shapes, shapes[1:])]
    centres = np.array([((x0 + x1) / 2, (y0 + y1) / 2) for x0, y0, x1, y1 in boxes])
    area = max(float(np.median(areas)), 1.0)
    pixels = max(float(np.median([np.count_nonzero(m) for m in masks])), 1.0)
    held = sum(np.mean([r.area for r in t.regions.values()]) for t in members if len(t.regions) >= life / 2)
    return np.array([
        life / n, math.log(life), math.log1p(len(members)),
        np.median(lengths) / life if len(lengths) else 0.0, lengths.max() / life if len(lengths) else 0.0, lengths.sum() / life,
        change, stats["aligned"], stats["flat"], stats["spread"], np.mean(outline) if outline else 0.0,
        math.log(area / (H * W)), np.median([np.count_nonzero(m) / max(1, m.size) for m in masks]),
        np.median(np.hypot(*np.diff(centres, axis=0).T)) / math.sqrt(area) if life > 1 else 0.0,
        np.mean([x0 <= 0 or y0 <= 0 or x1 >= W or y1 >= H for x0, y0, x1, y1 in boxes]),
        math.log1p(held / pixels), stats["contrast"],
    ], np.float32)


def _keep(x: np.ndarray, rule: bool) -> bool:
    """The learned judgement on a candidate's features, or `rule` without (or with unfit) weights."""
    from .assoc import load_matcher
    judge = load_matcher(str(WEIGHTS), N_FEATURES)
    return rule if judge is None else bool(judge.prob(x)[0] >= P_SOLID)


TITLE_SHARE = 0.5   # B11: a candidate holding a text box on this share of its frames is the rest of that title


def _holds_title(solid: Solid, text_boxes) -> bool:
    """B11: half a text box or more lies inside the candidate's box on TITLE_SHARE of its frames: the unread letters
    around a title's read pieces (ig2's scrolling "A Weekend Away"), not an object; its one texture cannot follow the
    letters passing through."""
    hits = sum(any(max(0, min(x1, X1) - max(x0, X0)) * max(0, min(y1, Y1) - max(y0, Y0)) >= 0.5 * max(1, (x1 - x0) * (y1 - y0))
                   for x0, y0, x1, y1 in (text_boxes[f] if f < len(text_boxes) else ()))
               for f, ((X0, Y0, X1, Y1), _) in solid.frames.items())
    return hits >= TITLE_SHARE * len(solid.frames)


def find_solids(frames, fg, obj_tracks, text_masks, min_life=12, min_members=3, change_thr=0.15, text_boxes=None) -> list[Solid]:
    """Track 5px-dilated components, retaining only cropped foreground support. `text_boxes`: per frame, the read text
    boxes (x0, y0, x1, y1), for B11. A candidate that lives long enough, is not a background and holds no title is
    judged by the learned judge (solids.npz) on its `_features`; without weights, by the rule: broken into short
    tracks, or a stable box whose look changes."""
    kernel = np.ones((11, 11), np.uint8)  # radius 5px
    floor = max(min_life, int(np.ceil(0.30 * len(frames))))
    min_area = max(1, int(np.ceil(0.001 * frames.shape[1] * frames.shape[2])))
    max_area = 0.60 * frames.shape[1] * frames.shape[2]
    candidates = {}
    serial = 0
    previous = []
    # ponytail: adjacent-frame one-to-one IoU; bridge occlusion gaps with a predictive component tracker.
    for f, (foreground, text) in enumerate(zip(fg, text_masks)):
        foreground = foreground.astype(bool) & ~text.astype(bool)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(cv2.dilate(foreground.astype(np.uint8), kernel))
        components = []
        for label in range(1, count):
            x, y, w, h, area = (int(v) for v in stats[label])
            if area < min_area:
                continue  # Exclude tiny dilated components before allocating masks or pairwise tracking.
            components.append(((x, y, x + w, y + h), labels[y:y + h, x:x + w] == label, area))
        pairs = []
        for i, (_, abox, a, aarea) in enumerate(previous):
            for j, (bbox, b, barea) in enumerate(components):
                x0, y0 = max(abox[0], bbox[0]), max(abox[1], bbox[1])
                x1, y1 = min(abox[2], bbox[2]), min(abox[3], bbox[3])
                if x1 <= x0 or y1 <= y0:
                    continue
                overlap = np.count_nonzero(a[y0 - abox[1]:y1 - abox[1], x0 - abox[0]:x1 - abox[0]] &
                                          b[y0 - bbox[1]:y1 - bbox[1], x0 - bbox[0]:x1 - bbox[0]])
                iou = overlap / max(1, aarea + barea - overlap)
                if iou >= 0.5:
                    pairs.append((iou, i, j))
        matches, used = {}, set()
        for _, i, j in sorted(pairs, reverse=True):
            if i not in used and j not in matches:
                matches[j] = previous[i][0]
                used.add(i)
        current = []
        for j, (bbox, component, area) in enumerate(components):
            cid = matches.get(j)
            if j not in matches and len(frames) - f >= floor:
                cid = serial
                serial += 1
                candidates[cid] = Solid()
            if cid is not None:
                x, y, x1, y1 = bbox
                mask = component & foreground[y:y1, x:x1]
                ys, xs = np.nonzero(mask)
                a, b, c, d = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
                box = (x + a, y + b, x + c, y + d)
                candidates[cid].frames[f] = (box, mask[b:d, a:c].copy())
            current.append((cid, bbox, component, area))
        active = {item[0] for item in current}
        for cid, *_ in previous:
            if cid is not None and cid not in active and len(candidates[cid].frames) < floor:
                del candidates[cid]
        previous = current
    result = []
    for solid in candidates.values():
        life = len(solid.frames)
        if life < floor:
            continue
        areas = [(box[2] - box[0]) * (box[3] - box[1]) for box, _ in solid.frames.values()]
        if np.median(areas) > max_area:
            continue  # Backgrounds/transitions are never objects, even with changing interiors or fragments.
        membership_masks = {f: cv2.dilate(np.pad(mask.astype(np.uint8), 5), kernel).astype(bool)
                            for f, (_, mask) in solid.frames.items()}
        members = []
        for track in obj_tracks:
            inside = 0
            for f, region in track.regions.items():
                if f in solid.frames:
                    mask = membership_masks[f]
                    x, y = (int(round(v)) for v in region.centroid)
                    x0, y0, _, _ = solid.frames[f][0]
                    mx, my = x - x0 + 5, y - y0 + 5
                    inside += (0 <= y < frames.shape[1] and 0 <= x < frames.shape[2] and
                               0 <= my < mask.shape[0] and 0 <= mx < mask.shape[1] and bool(mask[my, mx]))
            if inside >= len(track.regions) / 2:
                members.append(track)
        if text_boxes and _holds_title(solid, text_boxes):
            continue
        fragmented = len(members) >= min_members and np.median([len(t.regions) for t in members]) < life * 0.50
        stats = {}
        stable, change = _change(frames, solid, stats)
        rule = fragmented or (stable and change >= change_thr)
        try:
            keep = _keep(_features(frames, solid, members, change, stats), rule)
        except Exception as e:   # fail soft: the rule decides
            log.warning("learned solid judgement skipped (%s): %s", SOLID_JUDGE_FAILED, describe(e, trace=True))
            keep = rule
        if keep:
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
        mask = masks[f - 1].astype(np.uint8)
        if mask.shape != g0.shape:
            x0, y0, x1, y1 = boxes[f - 1]
            full = np.zeros(g0.shape, np.uint8)
            full[y0:y1, x0:x1] = mask
            mask = full
        pts = cv2.goodFeaturesToTrack(g0, 200, 0.01, 4, mask=mask)
        if pts is None:
            continue
        nxt, st, _ = cv2.calcOpticalFlowPyrLK(g0, g1, pts, None)
        if nxt is None or st is None:
            continue
        p, d = pts.reshape(-1, 2)[st.ravel() == 1], (nxt - pts).reshape(-1, 2)[st.ravel() == 1]
        x0, y0, x1, y1 = boxes[f - 1]; cx, cy, R = (x0 + x1) / 2, (y0 + y1) / 2, max(1.0, (x1 - x0 + y1 - y0) / 4)
        centres = [_centroid(masks[i]) + (np.array(boxes[i][:2]) if masks[i].shape != g0.shape else 0)
                   for i in (f - 1, f)]
        d = d - (centres[1] - centres[0])
        z = np.sqrt(np.maximum(R * R - (p[:, 0] - cx) ** 2 - (p[:, 1] - cy) ** 2, 0))
        ok = z >= 0.3 * R
        if ok.sum() >= 5:
            ry[f] += np.degrees(np.median(d[ok, 0] / z[ok]))
            rx[f] += np.degrees(np.median(d[ok, 1] / z[ok]))
    return rx, ry


def solid_props(solid, frames):
    cf = max(solid.frames, key=lambda f: np.count_nonzero(solid.frames[f][1]))
    (x0, y0, x1, y1), mask = solid.frames[cf]
    if mask.shape == frames.shape[1:3]:  # Read caches produced before cropped-mask tracking.
        mask = mask[y0:y1, x0:x1]
    canon = np.dstack([frames[cf, y0:y1, x0:x1], mask.astype(np.uint8) * 255])
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
