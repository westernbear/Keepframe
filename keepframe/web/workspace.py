from __future__ import annotations
import json, secrets, shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from keepframe.ir.store import load_project


def project_dir(workspace: Path, project_id: str) -> Path:
    return Path(workspace) / project_id

def list_projects(workspace: Path) -> list[dict]:
    rows = []
    for p in Path(workspace).iterdir() if Path(workspace).is_dir() else []:
        if not p.is_dir():
            continue
        row = load_meta(workspace, p.name)
        if row is not None:
            rows.append(row)
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
    root = project_dir(workspace, project_id)
    meta = root / "meta.json"
    if meta.is_file():
        return json.loads(meta.read_text(encoding="utf-8"))
    manifest = root / "project.json"
    if not manifest.is_file():
        return None
    try:
        project = load_project(root)
    except Exception:
        return None
    source = project.source
    source_file = source.get("file")
    title = Path(str(source_file)).stem if source_file else root.name
    latest = project.versions[-1].id if project.versions else None
    scene = project.scenes[0].id if project.scenes else None
    updated = datetime.fromtimestamp(manifest.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = {
        "id": root.name,
        "title": title or root.name,
        "status": "review",
        "updated": updated,
        "version": latest,
        "confidence": None,
        "scene": scene,
    }
    for key in ("mode", "range"):
        if key in source:
            row[key] = source[key]
    return row


def write_meta(workspace: Path, project_id: str, **updates: Any) -> dict:
    root = project_dir(workspace, project_id)
    meta_path = root / "meta.json"
    meta = load_meta(workspace, project_id)
    if meta is None:
        raise FileNotFoundError(root)
    meta.update(updates)
    meta["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
    return meta
