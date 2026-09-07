from __future__ import annotations
import cv2, numpy as np


def rgb_to_lab(img_rgb_uint8: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(np.ascontiguousarray(img_rgb_uint8), cv2.COLOR_RGB2LAB).astype(np.float32)


def estimate_background(frames: np.ndarray, k: int = 6) -> tuple[tuple[int, int, int], float]:
    sub = frames[::max(1, len(frames) // 12), ::4, ::4].reshape(-1, 3)
    lab = rgb_to_lab(sub.reshape(-1, 1, 3)).reshape(-1, 3)
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.5)
    _, labels, centers = cv2.kmeans(lab, k, None, crit, 3, cv2.KMEANS_PP_CENTERS)
    counts = np.bincount(labels.ravel(), minlength=k)
    mode = int(counts.argmax())
    rgb = sub[labels.ravel() == mode].mean(0)
    return tuple(int(round(v)) for v in rgb), float(counts[mode] / counts.sum())  # type: ignore[return-value]


def foreground_mask(frame: np.ndarray, bg_rgb: tuple[int, int, int], thr: float = 12.0) -> np.ndarray:
    lab = rgb_to_lab(frame)
    bg = rgb_to_lab(np.array(bg_rgb, np.uint8).reshape(1, 1, 3))[0, 0]
    return np.linalg.norm(lab - bg, axis=2) > thr
