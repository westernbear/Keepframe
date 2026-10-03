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
DEMO_REVISION = 2
DEMO_SCENE = f"synth{DEMO_SEED}"

_LOCK = threading.Lock()


def _complete(root: Path) -> bool:
    return (
        (root / "meta.json").is_file()
        and (root / "project.json").is_file()
        and (scene_dir(root, DEMO_SCENE) / "stages" / "frames.npy").is_file()
        and json.loads((root / "meta.json").read_text()).get("demo_revision") == DEMO_REVISION
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
        # Analyze the rendered pixels. The demo text box is a labelled fixture,
        # with no fabricated OCR confidence, and never uses reconstructed bounds.
        from dataclasses import asdict
        from keepframe.analyze.pipeline import (AnalyzeOptions, _stage_text, _stage_regions,
            _stage_tracking, _stage_sprites, _elements_from_props)
        from keepframe.review.overlay import snapshot_from_stages
        opts = AnalyzeOptions(refine=False, use_ecc=False)
        bg = (16, 20, 24)
        text_element = next(e for e in scene.elements if e.kind == "text")
        color = tuple(int(text_element.canonical.color[i:i+2], 16) for i in (1, 3, 5))
        def demo_ocr(frame):
            ys, xs = np.nonzero(np.all(frame == color, axis=2))
            if not len(xs):
                return []
            return [(text_element.canonical.text, (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1), None)]
        (stages / "background.json").write_text(json.dumps({"rgb": bg, "confidence": 1.0}))
        (stages / "options.json").write_text(json.dumps(asdict(opts)))
        boxes, text_tracks, _ = _stage_text(frames, bg, opts, demo_ocr, sd)
        regions = _stage_regions(frames, bg, boxes, opts, sd)
        tracks = _stage_tracking(regions, sd)
        props = _stage_sprites(frames, bg, text_tracks, tracks, opts, sd, len(frames))
        ids = {}
        scene.elements, _ = _elements_from_props(props, sd, ids)
        (stages / "ids.json").write_text(json.dumps(ids))
        init_project(root, {"file": "demo", "fps": scene.fps, "size": list(scene.size),
                     "mode": "range", "range": [0, scene.frames - 1]}, scene,
                     note="sample analysis", analysis_file=snapshot_from_stages(sd, scene))
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
            "demo_revision": DEMO_REVISION,
        }
        (root / "meta.json").write_text(json.dumps(row, indent=2, sort_keys=True), encoding="utf-8")
        return row
