from __future__ import annotations

import math
import secrets
import time
from pathlib import Path

SECONDS_PER_SCENE = 180

_tokens: dict[str, tuple[float, dict | None]] = {}


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


def estimate(
    mode: str,
    frames: int,
    fps: float,
    *,
    project_id: str | None = None,
    start: int = 0,
    end: int = -1,
    scenes: list[dict] | None = None,
    transitions: list[dict] | None = None,
    warnings: list[dict] | None = None,
    boundary_digest: str | None = None,
    local_analysis_seconds: float = 0.0,
) -> dict:
    duration_s = frames / fps if fps > 0 else 0.0
    scene_count = len(scenes) if scenes is not None else (1 if mode == "range" else max(1, math.ceil(duration_s / 4.0)))
    seconds = scene_count * SECONDS_PER_SCENE
    token = secrets.token_hex(8)
    context = (
        {
            "project_id": project_id,
            "mode": mode,
            "start": int(start),
            "end": int(end),
            "scenes": scenes,
            "transitions": transitions or [],
            "warnings": warnings or [],
            "boundary_digest": boundary_digest,
        }
        if project_id is not None
        else None
    )
    _tokens[token] = (time.time() + 30 * 60, context)
    return {
        "scene_count": scene_count,
        "seconds": seconds,
        "seconds_per_scene": SECONDS_PER_SCENE,
        "note": "추정. SLA 아님.",
        "confirm_token": token,
        "scenes": scenes or [],
        "transitions": transitions or [],
        "warnings": warnings or [],
        "boundary_digest": boundary_digest,
        "local_analysis_seconds": round(max(0.0, local_analysis_seconds), 3),
        "estimated_cost": {"currency": "USD", "amount": 0, "kind": "local"},
    }


def consume_token(
    token: str,
    *,
    project_id: str,
    mode: str,
    start: int,
    end: int,
    boundary_digest: str | None = None,
) -> bool:
    entry = _tokens.pop(token, None)
    if entry is None:
        return False
    expiry, context = entry
    if time.time() > expiry or context is None:
        return False
    return bool(
        context.get("project_id") == project_id
        and context.get("mode") == mode
        and context.get("start") == int(start)
        and context.get("end") == int(end)
        and context.get("boundary_digest") == boundary_digest
    )
