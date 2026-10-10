from __future__ import annotations
import shutil
from pathlib import Path
import cv2, numpy as np

PLATE_CONF_MAX = 0.30   # spec 5.2: dominant colour under 30% of pixels -> not a solid background
PLATE_RING = 0.08
PLATE_NONUNIFORM = 0.10
PLATE_PATH = "assets/background.png"
PASS1_PATH = "stages/plate_pass1.png"


def rgb_to_lab(img_rgb_uint8: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(np.ascontiguousarray(img_rgb_uint8), cv2.COLOR_RGB2LAB).astype(np.float32)


def estimate_background(frames: np.ndarray, k: int = 6) -> tuple[tuple[int, int, int], float]:
    sub = frames[::max(1, len(frames) // 12), ::4, ::4].reshape(-1, 3)
    lab = rgb_to_lab(sub.reshape(-1, 1, 3)).reshape(-1, 3)
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.5)
    cv2.setRNGSeed(0)
    _, labels, centers = cv2.kmeans(lab, k, None, crit, 3, cv2.KMEANS_PP_CENTERS)
    counts = np.bincount(labels.ravel(), minlength=k)
    mode = int(counts.argmax())
    rgb = sub[labels.ravel() == mode].mean(0)
    return tuple(int(round(v)) for v in rgb), float(counts[mode] / counts.sum())  # type: ignore[return-value]


def foreground_mask(frame: np.ndarray, bg_rgb: tuple[int, int, int], thr: float = 12.0) -> np.ndarray:
    lab = rgb_to_lab(frame)
    bg = rgb_to_lab(np.array(bg_rgb, np.uint8).reshape(1, 1, 3))[0, 0]
    return np.linalg.norm(lab - bg, axis=2) > thr


def background_plate(frames: np.ndarray) -> np.ndarray:
    """Low-pass temporal median of sampled frames.
    ponytail: static-camera MG only; large blurry static shapes are absorbed into the plate."""
    med = np.median(frames[::max(1, len(frames) // 24)], axis=0).astype(np.uint8)
    H, W = med.shape[:2]
    small = cv2.resize(med, (max(1, W // 32), max(1, H // 32)), interpolation=cv2.INTER_AREA)
    return cv2.resize(small, (W, H), interpolation=cv2.INTER_CUBIC)


def needs_plate(plate: np.ndarray, bg_rgb: tuple[int, int, int], conf: float) -> bool:
    if conf < PLATE_CONF_MAX:
        return True
    H, W = plate.shape[:2]
    b = max(2, int(PLATE_RING * min(H, W)))
    ring = np.ones((H, W), bool)
    ring[b:-b, b:-b] = False
    return bool(foreground_mask(plate, bg_rgb)[ring].mean() > PLATE_NONUNIFORM)


def foreground_mask_plate(frame: np.ndarray, plate: np.ndarray, thr: float = 12.0,
                          plate_lab: np.ndarray | None = None) -> np.ndarray:
    if plate_lab is None:
        plate_lab = rgb_to_lab(plate)
    return np.linalg.norm(rgb_to_lab(frame) - plate_lab, axis=2) > thr


def opacity_against_plate(pixels: np.ndarray, plate_pixels: np.ndarray, foreground_rgb) -> float:
    """Project matched float32 RGB pixels onto their local foreground contrast."""
    c = np.asarray(foreground_rgb, np.float32) - plate_pixels
    n = np.sum(c * c, axis=1)
    valid = n > 1e-6
    a = np.sum((pixels - plate_pixels) * c, axis=1)[valid] / n[valid]
    return float(np.clip(np.median(a), 0.0, 1.0)) if len(a) else 1.0


def pass1_plate(sd: Path) -> np.ndarray:
    """The scene's pass-1 plate. Before plate v2 it lived at assets/background.png; such a legacy file is
    copied to PASS1_PATH first, so a rebuilt v2 plate can take that name."""
    path = Path(sd) / PASS1_PATH
    if not path.is_file():
        legacy = Path(sd) / PLATE_PATH
        if not legacy.is_file():
            raise FileNotFoundError(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(legacy, path)
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(path)
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
