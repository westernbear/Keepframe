from __future__ import annotations
import json, secrets, shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

def project_dir(workspace: Path, project_id: str) -> Path:
    return Path(workspace) / project_id

def list_projects(workspace: Path) -> list[dict]:
    rows = []
    for p in Path(workspace).iterdir() if Path(workspace).exists() else []:
        meta = p / "meta.json"
        if not meta.exists():
            continue
        rows.append(json.loads(meta.read_text()))
    rows.sort(key=lambda r: r.get("updated", ""), reverse=True)
    return rows

def create_project(workspace: Path, title: str, video: Path, mode: str, range_: tuple[int, int] | None) -> dict:
    video = Path(video)
    if not video.exists():
        raise FileNotFoundError(video)
    if mode not in ("range", "full"):
        raise ValueError(mode)
    pid = "p" + secrets.token_hex(4)
    root = project_dir(workspace, pid)
    root.mkdir(parents=True)
    shutil.copy2(video, root / "source.mp4")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = {"id": pid, "title": title, "status": "uploaded", "updated": now,
           "version": None, "confidence": None, "mode": mode, "range": list(range_) if range_ else None}
    (root / "meta.json").write_text(json.dumps(row, indent=2, sort_keys=True))
    return row


def load_meta(workspace: Path, project_id: str) -> dict | None:
    meta = project_dir(workspace, project_id) / "meta.json"
    if not meta.is_file():
        return None
    return json.loads(meta.read_text(encoding="utf-8"))


def write_meta(workspace: Path, project_id: str, **updates: Any) -> dict:
    root = project_dir(workspace, project_id)
    meta_path = root / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.update(updates)
    meta["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
    return meta
