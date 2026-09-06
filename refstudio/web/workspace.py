from __future__ import annotations
import json, secrets, shutil
from datetime import datetime, timezone
from pathlib import Path

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
