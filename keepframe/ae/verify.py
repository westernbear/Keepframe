"""Compare After Effects frames with Keepframe's render of the same scene."""

import math
import tempfile
from pathlib import Path
from statistics import mean

import cv2
import numpy as np

from ..compose.composer import compose
from ..ir.schema import Scene
from ..render.renderer import render


VERIFY_MEAN_MAX = 0.025
VERIFY_FRAME_MAX = 0.04
MASK_PAD = 4


def sample_frames(frames: int, n: int = 16) -> list[int]:
    if frames <= n:
        return list(range(frames))
    return sorted({round(i * (frames - 1) / (n - 1)) for i in range(n)})


def compare_frames(
    ae: np.ndarray, kf: np.ndarray, masks: list[tuple[int, int, int, int]] = (),
) -> tuple[float, np.ndarray]:
    """Measure unmasked RGB L1; retain all differences in the amplified image."""
    if (ae.shape != kf.shape or ae.ndim != 3 or ae.shape[2] != 3
            or ae.dtype != np.uint8 or kf.dtype != np.uint8):
        raise ValueError("frames must be equal-sized HxWx3 uint8 images")
    delta = np.abs(ae.astype(np.int16) - kf.astype(np.int16))
    keep = np.ones(ae.shape[:2], dtype=bool)
    for left, top, right, bottom in masks:
        keep[max(0, top):max(0, bottom), max(0, left):max(0, right)] = False
    l1 = float(delta[keep].mean() / 255) if keep.any() else 0.0
    return l1, np.clip(delta * 4, 0, 255).astype(np.uint8)


def verify(
    scene: Scene, scene_dir: Path, ae_frames: dict[int, Path], out_dir: Path,
    *, masked: dict[str, dict[str, str]] = {},
) -> dict:
    """Render matching frames and write AE/KF/diff images for the worst three."""
    frames = sorted(ae_frames)
    if not frames:
        raise ValueError("no AE frames to compare")
    for frame in frames:
        if frame < 0 or frame >= scene.frames:
            raise ValueError(f"frame {frame} from AE is outside the scene")

    width, height = scene.size
    rows, worst_images = [], []
    masked_errors = {eid: 0.0 for eid in masked}
    with tempfile.TemporaryDirectory(prefix="keepframe-ae-verify-") as tmp:
        tmp = Path(tmp)
        html = compose(scene, scene_dir, tmp / "composition.html")
        rendered = render(html, scene, tmp / "render", frames=frames, probe=True)
        for i, frame in enumerate(frames):
            ae_path = Path(ae_frames[frame])
            ae = cv2.imread(str(ae_path), cv2.IMREAD_COLOR) if ae_path.is_file() else None
            if ae is None:
                raise ValueError(f"frame {frame} from AE is missing or not an image")
            kf_path = rendered.frames_dir / f"f_{i:05d}.png"
            kf = cv2.imread(str(kf_path), cv2.IMREAD_COLOR) if kf_path.is_file() else None
            if kf is None:
                raise ValueError(f"frame {frame} from Keepframe is missing or not an image")
            if ae.shape[:2] != (height, width):
                interpolation = (cv2.INTER_AREA if ae.shape[1] > width or ae.shape[0] > height
                                 else cv2.INTER_LINEAR)
                ae = cv2.resize(ae, scene.size, interpolation=interpolation)

            masks = []
            for eid in masked:
                left, top, right, bottom = rendered.bboxes[eid][i]
                if (left >= right or top >= bottom or right <= 0 or bottom <= 0
                        or left >= width or top >= height):
                    continue
                left, top = max(0, math.floor(left) - MASK_PAD), max(0, math.floor(top) - MASK_PAD)
                right = min(width, math.ceil(right) + MASK_PAD)
                bottom = min(height, math.ceil(bottom) + MASK_PAD)
                masks.append((left, top, right, bottom))
                region_l1, _ = compare_frames(ae[top:bottom, left:right], kf[top:bottom, left:right])
                masked_errors[eid] = max(masked_errors[eid], region_l1)

            l1, diff = compare_frames(ae, kf, masks)
            row = {"frame": frame, "l1": l1}
            rows.append(row)
            worst_images.append((row, {"ae": ae, "kf": kf, "diff": diff}))
            worst_images.sort(key=lambda item: item[0]["l1"], reverse=True)
            del worst_images[3:]

        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        worst = []
        for row, images in worst_images:
            entry = dict(row)
            for kind, image in images.items():
                name = f"f{row['frame']:04d}_{kind}.png"
                if not cv2.imwrite(str(out_dir / name), image):
                    raise OSError(f"could not write {name}")
                entry[kind] = name
            worst.append(entry)

    mean_l1 = mean(row["l1"] for row in rows)
    max_l1 = max(row["l1"] for row in rows)
    return {
        "passed": mean_l1 <= VERIFY_MEAN_MAX and max_l1 <= VERIFY_FRAME_MAX,
        "mean": mean_l1, "max": max_l1,
        "thresholds": {"mean": VERIFY_MEAN_MAX, "frame": VERIFY_FRAME_MAX},
        "frames": rows, "worst": worst,
        "notes": [f"{font['name']}: {font['font']} instead of {font['requested']}"
                  f" — text region differs by {masked_errors[eid] * 100:.1f}%"
                  for eid, font in masked.items()],
        "masked": [{"id": eid, **masked[eid], "worst_l1": l1} for eid, l1 in masked_errors.items()],
    }
