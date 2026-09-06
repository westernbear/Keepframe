# refstudio/analyze/regions.py
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
            if stats[c, cv2.CC_STAT_AREA] >= min_area:
                out.append(_region(frame_idx, frame, comp == c, int(lab)))
    return out
