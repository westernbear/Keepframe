import json

import cv2
import numpy as np
import pytest

from keepframe.compose.composer import compose
from keepframe.ir.schema import Keyframe, Track
from keepframe.ir.store import init_project
from keepframe.ir.synth import make_synthetic_scene
from keepframe.jobs.dispatch import run_job
from keepframe.render.lottie import compose_lottie_player, prepare_lottie_job, preflight_lottie, write_lottie
from keepframe.render.plan import PlanConflict, approve_render_plan, create_render_plan
from keepframe.render.renderer import render


def _scene(tmp_path):
    scene_dir = tmp_path / "scenes" / "s1"
    scene = make_synthetic_scene(scene_dir, seed=17, with_text=True, frames=12)
    return scene.model_copy(update={"id": "s1"}), scene_dir


def test_lottie_maps_embedded_assets_and_matches_native_frames(tmp_path):
    scene, scene_dir = _scene(tmp_path)
    animation_path = write_lottie(scene, scene_dir, tmp_path / "animation.json")
    animation = json.loads(animation_path.read_text(encoding="utf-8"))
    assert animation["fr"] == scene.fps
    assert animation["op"] == scene.frames
    assert animation["assets"]
    assert all(asset.get("layers") or asset.get("p", "").startswith("data:") for asset in animation["assets"])

    native_html = compose(scene, scene_dir, tmp_path / "native.html")
    lottie_html = compose_lottie_player(animation, tmp_path / "lottie.html")
    native = render(native_html, scene, tmp_path / "native", frames=[0, 8], probe=False)
    lottie = render(lottie_html, scene, tmp_path / "lottie", frames=[0, 8], probe=False)
    for index in range(2):
        left = cv2.imread(str(native.frames_dir / f"f_{index:05d}.png"), cv2.IMREAD_COLOR)
        right = cv2.imread(str(lottie.frames_dir / f"f_{index:05d}.png"), cv2.IMREAD_COLOR)
        assert float(np.mean(np.abs(left.astype(np.float32) - right.astype(np.float32))) / 255) < 0.3


def test_lottie_final_plan_exports_once_approved(tmp_path):
    root = tmp_path / "p1"
    scene, _ = _scene(root)
    init_project(root, {"file": "source.mp4", "fps": scene.fps, "size": list(scene.size)}, scene)
    (root / "meta.json").write_text(json.dumps({"id": "p1", "status": "approved", "scene": "s1", "version": "v1"}), encoding="utf-8")

    with pytest.raises(PlanConflict, match="final-only"):
        create_render_plan(root, project_id="p1", scene_id="s1", version_id="v1", backend="lottie", mode="preview")
    plan = create_render_plan(root, project_id="p1", scene_id="s1", version_id="v1", backend="lottie", mode="final")
    approve_render_plan(root, plan.id, digest=plan.digest, revision=0)
    result = run_job(prepare_lottie_job(root, plan.id))
    exported = json.loads(open(result["animation"], encoding="utf-8").read())
    assert exported["nm"] == "s1"
    assert plan.artifact_contract["outputs"] == ["animation", "report"]
    assert json.loads(open(result["report"], encoding="utf-8").read()) == {"warnings": []}


def test_lottie_preflight_rejects_unrepresentable_tracks(tmp_path):
    scene, _ = _scene(tmp_path)
    element = scene.elements[0].model_copy(update={"z": Track(keys=[Keyframe(t=0, v=0), Keyframe(t=1, v=1)])})
    with pytest.raises(PlanConflict, match="dynamic z"):
        preflight_lottie(scene.model_copy(update={"elements": [element, *scene.elements[1:]]}))


def test_lottie_exports_envato_shaped_sprite_with_stale_model_as_texture(tmp_path):
    scene, scene_dir = _scene(tmp_path)
    sprite = next(e for e in scene.elements if e.kind == "sprite")
    # envato1's rejected e46 retains a texture and a stale generated GLB path.
    sprite.id = "e46"
    sprite.canonical.model = "assets/e1.model1.glb"
    preflight_lottie(scene)
    animation = json.loads(write_lottie(scene, scene_dir, tmp_path / "animation.json").read_text())
    layer = next(layer for layer in animation["layers"] if layer["nm"] == sprite.id)
    assert layer["ty"] == 2
    asset = next(asset for asset in animation["assets"] if asset["id"] == layer["refId"])
    assert asset["p"].startswith("data:image/png;base64,")


def test_lottie_rejects_sprite_model_without_texture(tmp_path):
    scene, _ = _scene(tmp_path)
    sprite = next(e for e in scene.elements if e.kind == "sprite")
    sprite.canonical.model, sprite.canonical.texture = "assets/e1.model1.glb", None
    with pytest.raises(PlanConflict, match="3D element"):
        preflight_lottie(scene)
