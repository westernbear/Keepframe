from __future__ import annotations

import math
import secrets
import time
from pathlib import Path

SECONDS_PER_SCENE = 180

_tokens: dict[str, tuple[float, tuple[str, str, int, int] | None]] = {}


def probe_video(path: Path) -> dict:
    import cv2

    cap = cv2.VideoCapture(str(path))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = float(cap.get(cv2.CAP_PROP_FPS)) or 30.0
    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    duration_s = frames / fps if fps > 0 else 0.0
    return {
        "width": width,
        "height": height,
        "fps": fps,
        "frames": frames,
        "duration_s": duration_s,
    }


def estimate(mode: str, frames: int, fps: float, *, project_id: str | None = None, start: int = 0, end: int = -1) -> dict:
    duration_s = frames / fps if fps > 0 else 0.0
    if mode == "range":
        scene_count = 1
    else:
        scene_count = max(1, math.ceil(duration_s / 4.0))
    seconds = scene_count * SECONDS_PER_SCENE
    token = secrets.token_hex(8)
    context = (project_id, mode, int(start), int(end)) if project_id is not None else None
    _tokens[token] = (time.time() + 30 * 60, context)
    return {
        "scene_count": scene_count,
        "seconds": seconds,
        "seconds_per_scene": SECONDS_PER_SCENE,
        "note": "추정. SLA 아님.",
        "confirm_token": token,
    }


def consume_token(token: str, *, project_id: str, mode: str, start: int, end: int) -> bool:
    entry = _tokens.pop(token, None)
    if entry is None:
        return False
    expiry, context = entry
    return time.time() <= expiry and context == (project_id, mode, int(start), int(end))
