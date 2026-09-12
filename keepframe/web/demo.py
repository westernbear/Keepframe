from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from keepframe.analyze.composite import composite_scene
from keepframe.ir.store import init_project, scene_dir
from keepframe.ir.synth import make_synthetic_scene
from keepframe.web.workspace import project_dir

DEMO_ID = "demo"
DEMO_SEED = 7
DEMO_FRAMES = 24
DEMO_SCENE = f"synth{DEMO_SEED}"

_LOCK = threading.Lock()


def _complete(root: Path) -> bool:
    return (
        (root / "meta.json").is_file()
        and (root / "project.json").is_file()
        and (scene_dir(root, DEMO_SCENE) / "stages" / "frames.npy").is_file()
    )


def ensure_demo_project(workspace: Path) -> dict:
    """Idempotent MVP sample: synthetic scene + composited frames, status=review."""
    workspace = Path(workspace)
    root = project_dir(workspace, DEMO_ID)
    with _LOCK:
        if _complete(root):
            return json.loads((root / "meta.json").read_text(encoding="utf-8"))
        if root.exists():
            import shutil

            shutil.rmtree(root)
        workspace.mkdir(parents=True, exist_ok=True)
        sd = scene_dir(root, DEMO_SCENE)
        scene = make_synthetic_scene(sd, seed=DEMO_SEED, frames=DEMO_FRAMES, with_text=True)
        init_project(
            root,
            {
                "file": "demo",
                "fps": scene.fps,
                "size": list(scene.size),
                "mode": "range",
                "range": [0, scene.frames - 1],
            },
            scene,
            note="mvp sample",
        )
        stages = sd / "stages"
        stages.mkdir(parents=True, exist_ok=True)
        cache: dict = {}
        frames = np.stack(
            [
                (composite_scene(scene, sd, f, cache) * 255).round().clip(0, 255).astype(np.uint8)
                for f in range(scene.frames)
            ]
        )
        np.save(stages / "frames.npy", frames)
        l1 = [round(0.012 + 0.05 * ((i * 3) % 8) / 7, 4) for i in range(scene.frames)]
        (sd / "report.json").write_text(
            json.dumps({"reconstruction": {"per_frame_l1": l1}}, indent=2),
            encoding="utf-8",
        )
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = {
            "id": DEMO_ID,
            "title": "샘플 장면",
            "status": "review",
            "updated": now,
            "version": "v1",
            "confidence": None,
            "mode": "range",
            "range": [0, scene.frames - 1],
            "scene": scene.id,
            "demo": True,
        }
        (root / "meta.json").write_text(json.dumps(row, indent=2, sort_keys=True), encoding="utf-8")
        return row
