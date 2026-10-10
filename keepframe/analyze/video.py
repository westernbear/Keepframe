from __future__ import annotations
import shutil, subprocess, tempfile
from pathlib import Path
import cv2, numpy as np
from ..ir.schema import Scene
from .composite import composite_scene


def read_frames(path: Path, start: int = 0, end: int | None = None) -> tuple[np.ndarray, float]:
    """Decode into one preallocated stack (a list + np.stack held the frames twice)."""
    path = Path(path)
    if path.is_dir():
        files = sorted(path.glob("*.png"))[start: (end + 1) if end is not None else None]
        if not files:
            raise ValueError(f"no frames in {path} in [{start},{end}]")
        out = None
        for i, f in enumerate(files):
            rgb = cv2.cvtColor(cv2.imread(str(f), cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB)
            if out is None:
                out = np.empty((len(files), *rgb.shape), np.uint8)
            out[i] = rgb
        return out, 30.0
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise FileNotFoundError(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)
    stop = end + 1 if end is not None else None
    if total > start:
        stop = min(stop, total) if stop is not None else total
    out, n = None, 0
    while end is None or start + n <= end:
        ok, bgr = cap.read()
        if not ok:
            break
        if out is None:
            out = np.empty((max(1, (stop or 0) - start), *bgr.shape), np.uint8)
        elif n == len(out):   # container under-reported its frame count
            grown = np.empty((2 * n, *bgr.shape), np.uint8)
            grown[:n] = out
            out = grown
        out[n] = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB); n += 1
    cap.release()
    if not n:
        raise ValueError(f"no frames read from {path} in [{start},{end}]")
    return out[:n], float(fps)   # pages past n were never written, so they cost no memory


def write_video(frames: np.ndarray, fps: float, out: Path) -> Path:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH")
    out = Path(out); out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        for i, f in enumerate(frames):
            cv2.imwrite(f"{td}/f_{i:05d}.png", cv2.cvtColor(np.ascontiguousarray(f), cv2.COLOR_RGB2BGR))
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps), "-i", f"{td}/f_%05d.png",
                        "-c:v", "libx264", "-pix_fmt", "yuv444p", "-crf", "8", str(out)], check=True)
    return out


def render_scene_video(scene: Scene, scene_dir: Path, out: Path) -> Path:
    cache: dict = {}
    frames = np.stack([(composite_scene(scene, scene_dir, f, cache) * 255).round().clip(0, 255).astype(np.uint8)
                       for f in range(scene.frames)])
    return write_video(frames, scene.fps, out)
