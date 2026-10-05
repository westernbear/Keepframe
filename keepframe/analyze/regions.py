from __future__ import annotations
from dataclasses import dataclass
import cv2, numpy as np
from .background import rgb_to_lab

# ponytail: colour-cluster connected components. Upgrade path: Canny + trapped-ball (Motico §4.2) for gradients/outlines.


@dataclass
class Region:
    frame: int
    label: int
    color: tuple[float, float, float]
    bbox: tuple[int, int, int, int]
    area: int
    centroid: tuple[float, float]
    mask: np.ndarray


def build_palette(frames: np.ndarray, fg_masks: np.ndarray, k: int = 8) -> np.ndarray:
    step = max(1, len(frames) // 8)
    px = np.concatenate([frames[i][fg_masks[i]] for i in range(0, len(frames), step)])
    if len(px) == 0:
        return np.zeros((0, 3), np.float32)
    px = px[:: max(1, len(px) // 20000)]
    lab = rgb_to_lab(px.reshape(-1, 1, 3)).reshape(-1, 3)
    k = min(k, len(lab))
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.5)
    cv2.setRNGSeed(0)
    _, _, centers = cv2.kmeans(lab, k, None, crit, 3, cv2.KMEANS_PP_CENTERS)
    merged: list[np.ndarray] = []
    for c in centers:
        if all(np.linalg.norm(c - m) > 8 for m in merged):
            merged.append(c)
    return np.array(merged, np.float32)


def label_frame(frame: np.ndarray, fg_mask: np.ndarray, palette_lab: np.ndarray) -> np.ndarray:
    labels = np.full(fg_mask.shape, -1, np.int32)
    if len(palette_lab) == 0 or not fg_mask.any():
        return labels
    lab = rgb_to_lab(frame)[fg_mask]
    d = np.linalg.norm(lab[:, None, :] - palette_lab[None, :, :], axis=2)
    labels[fg_mask] = d.argmin(1)
    return labels


def _region(frame_idx: int, frame: np.ndarray, comp_mask: np.ndarray, label: int) -> Region:
    ys, xs = np.nonzero(comp_mask)
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1
    color = tuple(float(v) for v in frame[comp_mask].mean(0))
    return Region(frame=frame_idx, label=label, color=color, bbox=(x0, y0, x1, y1), area=int(comp_mask.sum()),
                  centroid=(float(xs.mean()), float(ys.mean())), mask=comp_mask[y0:y1, x0:x1].copy())


def _region_crop(frame_idx: int, frame: np.ndarray, sub: np.ndarray, x0: int, y0: int, label: int) -> Region:
    ys, xs = np.nonzero(sub)
    window = frame[y0:y0 + sub.shape[0], x0:x0 + sub.shape[1]]
    return Region(frame=frame_idx, label=label, color=tuple(float(v) for v in window[sub].mean(0)),
                  bbox=(x0 + int(xs.min()), y0 + int(ys.min()), x0 + int(xs.max()) + 1, y0 + int(ys.max()) + 1),
                  area=int(sub.sum()),
                  # Average global integer coordinates to preserve the full-frame mean's exact rounding.
                  centroid=(float((xs + x0).mean()), float((ys + y0).mean())),
                  mask=sub[ys.min():ys.max() + 1, xs.min():xs.max() + 1].copy())


def extract_regions(frame_idx: int, frame: np.ndarray, fg_mask: np.ndarray, palette_lab: np.ndarray, min_area: int = 30,
                    exclude_mask: np.ndarray | None = None,
                    overrides: list[tuple[np.ndarray, int]] | None = None) -> list[Region]:
    fg = fg_mask.copy()
    if exclude_mask is not None:
        fg &= ~exclude_mask
    out: list[Region] = []
    for mask, label in overrides or []:
        m = mask & fg
        if m.any():
            out.append(_region(frame_idx, frame, m, label)); fg &= ~mask
    labels = label_frame(frame, fg, palette_lab)
    for lab in np.unique(labels[labels >= 0]):
        n, comp, stats, _ = cv2.connectedComponentsWithStats((labels == lab).astype(np.uint8), connectivity=8)
        for c in range(1, n):
            x, y, w, h, area = (int(v) for v in stats[c])
            if area >= min_area:
                out.append(_region_crop(frame_idx, frame, comp[y:y + h, x:x + w] == c, x, y, int(lab)))
    return out


_K3 = np.ones((3, 3), np.uint8)


def _boxes_touch(a: Region, b: Region) -> bool:
    return a.bbox[0] <= b.bbox[2] and b.bbox[0] <= a.bbox[2] and a.bbox[1] <= b.bbox[3] and b.bbox[1] <= a.bbox[3]


def _edge_jump(a: Region, b: Region, lab: np.ndarray) -> float | None:
    """LAB distance across the shared edge; None when the masks do not touch."""
    H, W = lab.shape[:2]
    x0, y0 = max(0, max(a.bbox[0], b.bbox[0]) - 1), max(0, max(a.bbox[1], b.bbox[1]) - 1)
    x1, y1 = min(W, min(a.bbox[2], b.bbox[2]) + 1), min(H, min(a.bbox[3], b.bbox[3]) + 1)
    if x1 <= x0 or y1 <= y0:
        return None
    def place(r: Region) -> np.ndarray:
        m = np.zeros((y1 - y0, x1 - x0), np.uint8)
        rx0, ry0 = max(x0, r.bbox[0]), max(y0, r.bbox[1])
        rx1, ry1 = min(x1, r.bbox[2]), min(y1, r.bbox[3])
        if rx1 > rx0 and ry1 > ry0:
            m[ry0 - y0:ry1 - y0, rx0 - x0:rx1 - x0] = r.mask[
                ry0 - r.bbox[1]:ry1 - r.bbox[1], rx0 - r.bbox[0]:rx1 - r.bbox[0]]
        return m
    ma, mb = place(a), place(b)
    edge_a = (ma & cv2.dilate(mb, _K3)).astype(bool)
    edge_b = (mb & cv2.dilate(ma, _K3)).astype(bool)
    if not edge_a.any() or not edge_b.any():
        return None
    win = lab[y0:y1, x0:x1]
    return float(np.linalg.norm(win[edge_a].mean(0) - win[edge_b].mean(0)))


def merge_adjacent_regions(frame_idx: int, frame: np.ndarray, regions: list[Region], max_jump: float = 3.0) -> list[Region]:
    """Union touching regions with no colour jump at their shared edge (palette bands of one gradient fill).
    ponytail: edge-jump test only; max_jump=3 because anti-aliased sprite edges merge above ~3 Lab.
    Anti-alias rings stay separate (min_area drops most), Canny + trapped-ball (Motico §4.2) next."""
    if len(regions) < 2:
        return regions
    lab = rgb_to_lab(frame)
    parent = list(range(len(regions)))
    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]; i = parent[i]
        return i
    for i, a in enumerate(regions):
        for j in range(i + 1, len(regions)):
            b = regions[j]
            if a.label >= 1000 or b.label >= 1000 or not _boxes_touch(a, b):
                continue                                  # manual override regions never merge
            jump = _edge_jump(a, b, lab)
            if jump is not None and jump <= max_jump:
                parent[find(i)] = find(j)
    groups: dict[int, list[Region]] = {}
    for i, r in enumerate(regions):
        groups.setdefault(find(i), []).append(r)
    out = []
    for members in groups.values():
        if len(members) == 1:
            out.append(members[0]); continue
        x0, y0 = min(r.bbox[0] for r in members), min(r.bbox[1] for r in members)
        x1, y1 = max(r.bbox[2] for r in members), max(r.bbox[3] for r in members)
        mask = np.zeros((y1 - y0, x1 - x0), bool)
        for r in members:
            mask[r.bbox[1] - y0:r.bbox[3] - y0, r.bbox[0] - x0:r.bbox[2] - x0] |= r.mask
        ys, xs = np.nonzero(mask)
        color = tuple(float(v) for v in frame[y0:y1, x0:x1][mask].mean(0))
        out.append(Region(frame=frame_idx, label=max(members, key=lambda r: r.area).label, color=color,
                          bbox=(x0, y0, x1, y1), area=int(mask.sum()),
                          centroid=(float((xs + x0).mean()), float((ys + y0).mean())), mask=mask))
    return out
