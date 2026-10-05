"""Prepare the three round-2 AE checks locally; never connect to an AE host.

Run from this checkout with .venv/bin/python scripts/ae_live_check.py --clip
/home/singlerr/ref_stdio/eval/clips/ig2.mp4 --workspace /path/to/workspace.
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import secrets
import shutil
import sys
import tempfile

import cv2
import numpy as np

# This developer kit deliberately uses the same GLB fixture as the test suite.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_three import _triangle_glb

from keepframe.analyze.pipeline import AnalyzeOptions, analyze
from keepframe.compose.composer import compose
from keepframe.ir.schema import Background, Canonical, Constraint, Element, Keyframe, Scene, Track
from keepframe.ir.store import current_scene, init_project, save_scene, scene_dir
from keepframe.ir.synth import make_text_texture
from keepframe.render.renderer import render
from keepframe.web.workspace import write_meta

DEFAULT_CLIP = Path("/home/singlerr/ref_stdio/eval/clips/ig2.mp4")


def _track(*keys: tuple[int, float]) -> Track:
    return Track(keys=[Keyframe(t=frame, v=value) for frame, value in keys])


def _synthetic(root: Path, check: str) -> Scene:
    sd = scene_dir(root, "s1")
    assets = sd / "assets"
    assets.mkdir(parents=True)
    if check == "reveal":
        width, height = make_text_texture(assets / "text.png", "KEEPFRAME", 48, (255, 255, 255))
        element = Element(
            id="text", kind="text", role="text", visible=(0, 60),
            canonical=Canonical(width=width, height=height, text="KEEPFRAME", texture="assets/text.png"),
            tracks={"x": _track((0, 320)), "y": _track((0, 180)), "reveal": _track((0, 0), (30, 1))},
        )
        constraints = [Constraint(pred="type(m_text_1, reveal)", keep=True)]
    else:
        (assets / "triangle.glb").write_bytes(_triangle_glb())
        element = Element(
            id="model", kind="3d", visible=(0, 60),
            canonical=Canonical(width=192, height=192, model="assets/triangle.glb"),
            tracks={"x": _track((0, 320)), "y": _track((0, 180)), "ry": _track((0, 0), (60, 360))},
        )
        constraints = [Constraint(pred=pred, keep=True) for pred in (
            "type(m_model_1, spin)", "mag(m_model_1, 360)", "dur(m_model_1, 60)",
        )]
    scene = Scene(id="s1", size=(640, 360), fps=30, frames=61,
        background=Background(value="#101418"), elements=[element], constraints=constraints)
    init_project(root, {"file": f"synthetic-{check}", "fps": 30, "size": [640, 360],
        "mode": "range", "range": [0, 60]}, scene, note=f"Task 21: {check} check")
    return scene


def _references(root: Path, scene: Scene, frames: list[int], *, synthetic: bool) -> dict[str, str]:
    sd = scene_dir(root, scene.id)
    output = sd / "live-check"
    html = compose(scene, sd, output / "composition.html")
    rendered_frames = list(range(scene.frames)) if synthetic else frames
    result = render(html, scene, output / "native", frames=rendered_frames, probe=False)
    references = {str(frame): str((result.frames_dir / f"f_{index:05d}.png").relative_to(root))
        for index, frame in enumerate(rendered_frames) if frame in frames}
    if synthetic:
        # Review's Original pane reads these native reference pixels at any
        # scene frame, including the synthetic model's full turn.
        stages = sd / "stages"
        stages.mkdir(exist_ok=True)
        pixels = np.lib.format.open_memmap(stages / "frames.npy", mode="w+", dtype=np.uint8,
            shape=(scene.frames, scene.size[1], scene.size[0], 3))
        for index in rendered_frames:
            pixels[index] = cv2.cvtColor(cv2.imread(str(result.frames_dir / f"f_{index:05d}.png")), cv2.COLOR_BGR2RGB)
        if scene.elements[0].kind == "3d":
            # ponytail: the fallback is the frame-zero orthographic view, not
            # animated model depth; native AE model import is the upgrade path.
            crop = np.asarray(pixels[0, 84:276, 224:416]).copy()
            alpha = np.where(np.any(crop != (16, 20, 24), axis=2), 255, 0).astype(np.uint8)
            rgba = np.dstack([crop, alpha])
            texture = "assets/static-model.png"
            if not cv2.imwrite(str(sd / texture), cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA)):
                raise RuntimeError("could not write the model's static texture")
            scene.elements[0].canonical.texture = texture
            save_scene(scene, sd / "scene.v1.json")
        pixels.flush()
        del pixels
    return references


def prepare_checks(clip: Path, workspace: Path | None = None, *, ig2_frames: int = 120) -> dict:
    clip = Path(clip).resolve(strict=True)
    if not clip.is_file():
        raise ValueError("clip must be a video file")
    if ig2_frames < 2:
        raise ValueError("ig2_frames must be at least two")
    capture = cv2.VideoCapture(str(clip))
    try:
        if not capture.isOpened():
            raise ValueError("clip could not be opened")
        count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    finally:
        capture.release()
    if count < 2:
        raise ValueError("clip must contain at least two frames")
    workspace = Path(workspace).resolve() if workspace is not None else Path(tempfile.mkdtemp(prefix="keepframe-ae-live-"))
    workspace.mkdir(parents=True, exist_ok=True)
    run_id = secrets.token_hex(4)
    projects = []
    for check in ("reveal", "plate", "model"):
        project_id = f"ae-live-{check}-{run_id}"
        root = workspace / project_id
        root.mkdir()  # Never overwrite an existing project in a served workspace.
        print(f"Preparing {check}: {project_id}", file=sys.stderr, flush=True)
        if check == "plate":
            shutil.copy2(clip, root / "source.mp4")
            end = min(ig2_frames, count) - 1
            # ponytail: first 120 frames, no optional OCR/GPU refinement or ECC;
            # use --ig2-frames for a longer check, or the review reanalysis flow.
            analyze(root / "source.mp4", 0, end, root,
                AnalyzeOptions(ocr=False, refine=False, use_ecc=False))
            scene, _version = current_scene(root, "s1")
            if scene.background.kind != "image":
                raise ValueError("plate check analysis did not produce an image background; inspect this clip/range")
            frames = sorted({0, end // 4, end // 2, end * 3 // 4, end})
        else:
            scene = _synthetic(root, check)
            frames = [0, 7, 15, 22, 30, 60] if check == "reveal" else [0, 10, 20, 30, 40, 50, 60]
        native_frames = _references(root, scene, frames, synthetic=check != "plate")
        write_meta(workspace, project_id, title=f"AE live check: {check}", status="review", version="v1", scene=scene.id)
        query = f"project={project_id}&scene={scene.id}&v=v1"
        projects.append({"check": check, "project_id": project_id, "scene_id": scene.id,
            "review_url": f"/review?{query}", "ae_export_url": f"/agent?{query}",
            "frames": frames, "fps": scene.fps, "native_frames": native_frames})
    manifest = workspace / f"ae-live-check-{run_id}.json"
    output = {"workspace": str(workspace), "manifest": str(manifest), "projects": projects}
    manifest.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clip", type=Path, default=DEFAULT_CLIP)
    parser.add_argument("--workspace", type=Path, help="served workspace; defaults to a new temp directory")
    parser.add_argument("--ig2-frames", type=int, default=120, help="analyze the first N clip frames (default: 120)")
    args = parser.parse_args(argv)
    # Keep stdout as one machine-readable manifest even when analysis logs.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, force=True)
    output = prepare_checks(args.clip, args.workspace, ig2_frames=args.ig2_frames)
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
