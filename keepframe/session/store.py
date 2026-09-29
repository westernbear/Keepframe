from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_HISTORY_TURNS = 20
MAX_HISTORY_BYTES = 64 * 1024

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def scene_lock(root: Path, scene_id: str) -> threading.RLock:
    key = str(Path(root).resolve() / "sessions" / scene_id)
    with _locks_guard:
        return _locks.setdefault(key, threading.RLock())


def session_path(root: Path, scene_id: str) -> Path:
    if not scene_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in scene_id):
        raise ValueError("invalid scene id")
    return Path(root) / "sessions" / f"{scene_id}.jsonl"


def _safe_directory(root: Path, *, create: bool = False) -> Path:
    root = Path(root).resolve(strict=True)
    directory = root / "sessions"
    if create:
        directory.mkdir(parents=False, exist_ok=True)
    if directory.is_symlink() or (directory.exists() and (not directory.is_dir() or directory.resolve() != directory)):
        raise ValueError("session directory is unsafe")
    return directory


def _records(root: Path, scene_id: str) -> list[dict[str, Any]]:
    path = _safe_directory(root) / session_path(root, scene_id).name
    if not path.is_file() or path.is_symlink():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("schema") == "keepframe.session-turn/1":
            records.append(value)
    return records


def append_turn(root: Path, scene_id: str, user: str, turn) -> dict[str, Any]:
    record = {
        "schema": "keepframe.session-turn/1",
        "id": secrets.token_hex(12),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "user": user,
        "turn": turn.to_json(),
    }
    payload = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    path = _safe_directory(root, create=True) / session_path(root, scene_id).name
    if path.is_symlink():
        raise ValueError("session transcript is unsafe")
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        remaining = memoryview(payload)
        while remaining:
            remaining = remaining[os.write(descriptor, remaining):]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return record


def page(root: Path, scene_id: str, *, before: str | None = None, limit: int = 50) -> dict[str, Any]:
    records = _records(root, scene_id)
    stop = len(records)
    if before:
        stop = next((index for index, record in enumerate(records) if record.get("id") == before), stop)
    limit = max(1, min(int(limit), 100))
    start = max(0, stop - limit)
    selected = records[start:stop]
    latest_pending = next(
        (record.get("id") for record in reversed(records) if (record.get("turn") or {}).get("status") == "pending"),
        None,
    )
    visible = []
    for record in selected:
        item = json.loads(json.dumps(record))
        pending = (item.get("turn") or {}).get("status") == "pending"
        item["actionable"] = bool(pending and item.get("id") == latest_pending)
        item["read_only"] = bool(pending and not item["actionable"])
        visible.append(item)
    return {"turns": visible, "next_before": records[start]["id"] if start else None}


def llm_history(root: Path, scene_id: str) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    used = 0
    for record in reversed(_records(root, scene_id)):
        if len(selected) >= MAX_HISTORY_TURNS:
            break
        size = len(json.dumps(record, ensure_ascii=False).encode("utf-8"))
        if used + size > MAX_HISTORY_BYTES:
            break
        selected.append(record)
        used += size
    history: list[dict[str, Any]] = []
    for record in reversed(selected):
        history.append({"role": "user", "content": str(record.get("user") or "")})
        turn = record.get("turn") or {}
        content = str(turn.get("reply") or "")
        if turn.get("results"):
            content += "\n도구 결과: " + json.dumps(turn["results"], ensure_ascii=False, separators=(",", ":"))
        history.append({"role": "assistant", "content": content})
    return history
