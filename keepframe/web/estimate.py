from __future__ import annotations

import math
import secrets
import time
from pathlib import Path

SECONDS_PER_SCENE = 180

_tokens: dict[str, float] = {}


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


def estimate(mode: str, frames: int, fps: float) -> dict:
    duration_s = frames / fps if fps > 0 else 0.0
    if mode == "range":
        scene_count = 1
    else:
        scene_count = max(1, math.ceil(duration_s / 4.0))
    seconds = scene_count * SECONDS_PER_SCENE
    token = secrets.token_hex(8)
    _tokens[token] = time.time() + 30 * 60
    return {
        "scene_count": scene_count,
        "seconds": seconds,
        "seconds_per_scene": SECONDS_PER_SCENE,
        "note": "추정. SLA 아님.",
        "confirm_token": token,
    }


def consume_token(token: str) -> bool:
    expiry = _tokens.get(token)
    if expiry is None:
        return False
    if time.time() > expiry:
        del _tokens[token]
        return False
    del _tokens[token]
    return True
