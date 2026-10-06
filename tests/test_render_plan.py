import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

import keepframe.render.plan as render_plan_module

from keepframe.ir.store import init_project, init_project_scenes, save_scene
from keepframe.ir.synth import make_synthetic_scene
from keepframe.render.plan import (
    PlanConflict,
    RenderPlan,
    _plan_digest,
    approve_render_plan,
    create_render_plan,
    load_render_plan,
    load_render_plan_scene,
)


def _project(tmp_path: Path, *, approved: bool = False):
    root = tmp_path / "p1"
    scene = make_synthetic_scene(root / "scenes" / "s1", seed=7, with_text=False, frames=12)
    scene = scene.model_copy(update={"id": "s1"})
    init_project(
        root,
        {"file": "source.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, 11]},
        scene,
    )
    (root / "meta.json").write_text(
        json.dumps({"id": "p1", "status": "approved" if approved else "review", "version": "v1", "scene": "s1"}),
        encoding="utf-8",
    )
    return root, scene


def test_plan_is_canonical_immutable_and_pins_assets(tmp_path):
    root, scene = _project(tmp_path)

    plan = create_render_plan(
        root,
        project_id="p1",
        scene_id="s1",
        version_id="v1",
        backend="native",
        mode="preview",
    )

    stored = load_render_plan(root, plan.id)
    assert stored == plan
    assert stored.digest == plan.digest
    assert stored.scene_sha256
    assert stored.assets
    for asset in stored.assets:
        pinned = root / "renders" / plan.id / "assets" / asset.id
        assert pinned.read_bytes() == (root / asset.project_path).read_bytes()
        assert asset.sha256 == asset.id.split(".", 1)[0]
        assert asset.length == pinned.stat().st_size
    with pytest.raises(ValidationError):
        RenderPlan.model_validate({**plan.model_dump(mode="json"), "backend": "unknown"})
    with pytest.raises(ValidationError):
        plan.backend = "lottie"


def test_plan_loads_immutable_scene_snapshot_after_source_changes(tmp_path):
    root, scene = _project(tmp_path)
    plan = create_render_plan(
        root,
        project_id="p1",
        scene_id="s1",
        version_id="v1",
        backend="native",
        mode="preview",
    )
    source = root / plan.scene_project_path
    save_scene(scene.model_copy(update={"frames": scene.frames + 1}), source)

    pinned = load_render_plan_scene(root, plan.id)

    assert pinned == scene
    assert hashlib.sha256(
        (root / "renders" / plan.id / "scene.json").read_bytes()
    ).hexdigest() == plan.scene_sha256
    snapshot = root / "renders" / plan.id / "scene.json"
    snapshot.write_bytes(b"{}")
    with pytest.raises(PlanConflict, match="digest"):
        load_render_plan_scene(root, plan.id)


def test_plan_rejects_asset_escape_and_symlink(tmp_path):
    root, scene = _project(tmp_path)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")
    element = scene.elements[0].model_copy(
        update={"canonical": scene.elements[0].canonical.model_copy(update={"texture": "../../outside.png"})}
    )
    escaped = scene.model_copy(update={"elements": [element, *scene.elements[1:]]})
    save_scene(escaped, root / "scenes" / "s1" / "scene.v1.json")
    with pytest.raises(ValueError, match="project-relative"):
        create_render_plan(root, project_id="p1", scene_id="s1", version_id="v1", backend="native", mode="preview")

    save_scene(scene, root / "scenes" / "s1" / "scene.v1.json")
    texture_path = scene.elements[0].canonical.texture
    assert texture_path is not None
    texture = root / "scenes" / "s1" / texture_path
    texture.unlink()
    texture.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        create_render_plan(root, project_id="p1", scene_id="s1", version_id="v1", backend="native", mode="preview")


def test_final_plan_requires_exact_approved_version(tmp_path):
    root, _ = _project(tmp_path)
    with pytest.raises(PlanConflict, match="approved"):
        create_render_plan(root, project_id="p1", scene_id="s1", version_id="v1", backend="native", mode="final")
    meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
    meta.update(status="approved", version="v2")
    (root / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(PlanConflict, match="version"):
        create_render_plan(root, project_id="p1", scene_id="s1", version_id="v1", backend="native", mode="final")


def test_final_plan_uses_scene_specific_approval(tmp_path):
    root = tmp_path / "p1"
    first = make_synthetic_scene(root / "scenes" / "s1", seed=31, with_text=False, frames=6).model_copy(update={"id": "s1"})
    second = make_synthetic_scene(root / "scenes" / "s2", seed=32, with_text=False, frames=6).model_copy(update={"id": "s2"})
    init_project_scenes(root, {"file": "source.mp4", "fps": 30, "size": list(first.size)}, [(first, (0, 5), {"transition": "cut"}), (second, (6, 11), None)])
    (root / "meta.json").write_text(json.dumps({"id": "p1", "status": "approved", "scene": "s2", "version": "v1", "approved_scenes": {"s1": "v1", "s2": "v1"}}), encoding="utf-8")

    plan = create_render_plan(root, project_id="p1", scene_id="s1", version_id="v1", backend="native", mode="final")
    assert plan.scene_id == "s1"


def test_approval_is_consume_once_and_idempotent(tmp_path):
    root, _ = _project(tmp_path)
    plan = create_render_plan(root, project_id="p1", scene_id="s1", version_id="v1", backend="native", mode="preview")

    first = approve_render_plan(root, plan.id, digest=plan.digest, revision=0)
    retry = approve_render_plan(root, plan.id, digest=plan.digest, revision=0)
    assert retry == first
    assert first.status == "approved"
    assert first.execution_id
    with pytest.raises(PlanConflict, match="digest"):
        approve_render_plan(root, plan.id, digest="0" * 64, revision=0)
    with pytest.raises(PlanConflict, match="revision"):
        approve_render_plan(root, plan.id, digest=plan.digest, revision=9)


def test_full_operation_manifest_is_pinned_by_plan_digest(tmp_path):
    root, _ = _project(tmp_path)
    manifest = ({"name": "render", "schema": {"title": "Native render contract"}},)
    plan = create_render_plan(
        root,
        project_id="p1",
        scene_id="s1",
        version_id="v1",
        backend="native",
        mode="preview",
        permitted_operations=manifest,
    )

    assert plan.permitted_operations == manifest
    assert _plan_digest(plan) == plan.digest

    changed_manifest = json.loads(json.dumps(manifest))
    changed_manifest[0]["schema"]["title"] = "Changed operation contract"
    changed = plan.model_copy(update={"permitted_operations": tuple(changed_manifest)})
    assert _plan_digest(changed) != plan.digest

    plan_path = root / "renders" / plan.id / "plan.json"
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    payload["permitted_operations"][0]["schema"]["title"] = "Changed operation contract"
    plan_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PlanConflict, match="digest"):
        load_render_plan(root, plan.id)


def test_substitution_assets_and_duplicate_aliases_remain_in_manifest(tmp_path):
    root, scene = _project(tmp_path)
    first_texture = scene.elements[0].canonical.texture
    assert first_texture is not None
    alias = root / "scenes" / "s1" / "assets" / "alias.png"
    alias.write_bytes((root / "scenes" / "s1" / first_texture).read_bytes())
    second = scene.elements[1].model_copy(
        update={"canonical": scene.elements[1].canonical.model_copy(update={"texture": "assets/alias.png"})}
    )
    save_scene(
        scene.model_copy(update={"elements": [scene.elements[0], second, *scene.elements[2:]]}),
        root / "scenes" / "s1" / "scene.v1.json",
    )
    substitution = root / "replacement.png"
    substitution.write_bytes(b"replacement")

    plan = create_render_plan(
        root,
        project_id="p1",
        scene_id="s1",
        version_id="v1",
        backend="native",
        mode="preview",
        substitutions=({"asset_path": "replacement.png"},),
        substitutions_acknowledged=True,
    )

    by_path = {asset.project_path: asset for asset in plan.assets}
    original = f"scenes/s1/{first_texture}"
    assert by_path[original].id == by_path["scenes/s1/assets/alias.png"].id
    assert by_path["replacement.png"].role == "substitution"


def test_approval_revalidates_scene_and_final_metadata(tmp_path):
    root, _ = _project(tmp_path, approved=True)
    preview = create_render_plan(
        root, project_id="p1", scene_id="s1", version_id="v1", backend="native", mode="preview"
    )
    scene_path = root / "scenes" / "s1" / "scene.v1.json"
    scene_path.write_text(scene_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(PlanConflict, match="scene"):
        approve_render_plan(root, preview.id, digest=preview.digest, revision=0)

    root2, _ = _project(tmp_path / "other", approved=True)
    final = create_render_plan(
        root2, project_id="p1", scene_id="s1", version_id="v1", backend="native", mode="final"
    )
    meta = json.loads((root2 / "meta.json").read_text(encoding="utf-8"))
    meta["status"] = "review"
    (root2 / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(PlanConflict, match="approved"):
        approve_render_plan(root2, final.id, digest=final.digest, revision=0)


def test_render_directory_reparse_points_are_rejected(tmp_path, monkeypatch):
    root, _ = _project(tmp_path)
    original = render_plan_module._reparse_point
    monkeypatch.setattr(
        render_plan_module,
        "_reparse_point",
        lambda path: path.name == "renders" or original(path),
    )
    with pytest.raises(PlanConflict, match="renders"):
        create_render_plan(
            root, project_id="p1", scene_id="s1", version_id="v1", backend="native", mode="preview"
        )


def test_approval_fails_closed_without_platform_file_lock(tmp_path, monkeypatch):
    root, _ = _project(tmp_path)
    plan = create_render_plan(
        root, project_id="p1", scene_id="s1", version_id="v1", backend="native", mode="preview"
    )
    real_import = __import__

    def without_lock(name, *args, **kwargs):
        if name in {"fcntl", "msvcrt"}:
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", without_lock)
    with pytest.raises(PlanConflict, match="lock"):
        approve_render_plan(root, plan.id, digest=plan.digest, revision=0)


@pytest.mark.parametrize("backend", ["native", "lottie"])
def test_stored_local_plan_with_retired_empty_metadata_keeps_approval(tmp_path, backend):
    root, _ = _project(tmp_path, approved=True)
    plan = create_render_plan(root, project_id="p1", scene_id="s1", version_id="v1",
                              backend=backend, mode="final")
    plan_path = root / "renders" / plan.id / "plan.json"
    payload = json.loads(plan_path.read_text())
    payload.update(effect_schemas=[], capability_hash=None, capability_manifest=None,
                   predecessor_id=None, predecessor_digest=None, predecessor_checkpoint=None,
                   predecessor_checkpoint_digest=None)
    payload["digest"] = _plan_digest(payload)
    plan_path.write_text(json.dumps(payload))
    state_path = plan_path.with_name("state.json")
    state = json.loads(state_path.read_text())
    state["digest"] = payload["digest"]
    state_path.write_text(json.dumps(state))

    loaded = load_render_plan(root, plan.id)
    assert loaded.backend == backend
    assert loaded.digest == payload["digest"]
    approved = approve_render_plan(root, plan.id, digest=loaded.digest, revision=0)
    assert approved.status == "approved"
    assert approved.execution_id
    payload["direction"] = "unapproved change"
    plan_path.write_text(json.dumps(payload))
    with pytest.raises(PlanConflict, match="digest"):
        load_render_plan(root, plan.id)
