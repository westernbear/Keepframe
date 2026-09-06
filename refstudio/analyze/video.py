from __future__ import annotations
import shutil, subprocess, tempfile
from pathlib import Path
import cv2, numpy as np
from ..ir.schema import Scene
from .composite import composite_scene


def read_frames(path: Path, start: int = 0, end: int | None = None) -> tuple[np.ndarray, float]:
    path = Path(path)
    if path.is_dir():
        files = sorted(path.glob("*.png"))[start: (end + 1) if end is not None else None]
        frames = [cv2.cvtColor(cv2.imread(str(f), cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB) for f in files]
        return np.stack(frames), 30.0
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise FileNotFoundError(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)
    frames, i = [], start
    while True:
        ok, bgr = cap.read()
        if not ok or (end is not None and i > end):
            break
        frames.append(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)); i += 1
    cap.release()
    if not frames:
        raise ValueError(f"no frames read from {path} in [{start},{end}]")
    return np.stack(frames), float(fps)


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
