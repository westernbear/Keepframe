import json
from unittest.mock import Mock

import pytest

from keepframe.analyze.constraints import extract_constraints
from keepframe.edit.agent import edit
from keepframe.ir.schema import Background, Constraint, Element, Canonical, Scene
from keepframe.ir.store import current_scene, init_project, load_project, scene_dir
from keepframe.ir.synth import make_synthetic_scene
from keepframe.session.tools import SessionContext, run_tool


def _ctx(root, scene_id="s1", version=None):
    return SessionContext(root=root, scene_id=scene_id, version=version)


def _keep_project(tmp_path, *, extra_constraints=()):
    root = tmp_path / "proj"
    sd = root / "scenes" / "s1"
    scene = make_synthetic_scene(sd, seed=2, with_text=False, frames=12)
    scene = scene.model_copy(update={"id": "s1"})
    scene.constraints = [
        Constraint(pred="type(e1,'translation')", keep=True),
        Constraint(pred="left(e1,e2)", keep=True),
        Constraint(pred="right(e10,e2)", keep=False),
        Constraint(pred="type(e2,'translation')", keep=False),
    ]
    scene.constraints.extend(extra_constraints)
    init_project(root, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, 11]}, scene)
    return root, scene


@pytest.mark.parametrize("targets,on,matched", [
    (["e1"], False, 3),
    (["type(e1,'translation')"], True, 1),
    (["e1", "left(e1,e2)", "e1"], True, 3),
    (["e"], False, 4),
])
def test_set_keep_previews_matching_constraints_without_mutation(tmp_path, targets, on, matched):
    root, scene = _keep_project(tmp_path)
    before = load_project(root).model_dump()

    res = run_tool("set_keep", _ctx(root), {"targets": targets, "on": on, "confirm": True})
    assert res["ok"] is True
    assert res["needs_confirm"] is True
    assert res["needs_choice"] is False
    examples = [c.pred for c in scene.constraints if any(t in c.pred for t in targets)][:5]
    assert res["payload"] == {"keep_change": {"targets": targets, "on": on, "matched": matched, "examples": examples}}
    assert load_project(root).model_dump() == before
    unchanged, version = current_scene(root, "s1")
    assert version.id == "v1"
    assert unchanged.constraints == scene.constraints


@pytest.mark.parametrize("targets", [
    "e1", ["e1", 1], ["e1", None], ["e1", {}], [""],
    ["e1"] * 201, ["e1", "x" * 201], None, {"e1": True}, ("e1",),
], ids=["string", "number_item", "null_item", "object_item", "empty_item",
        "too_many", "too_long", "null", "object", "tuple"])
def test_set_keep_rejects_malformed_targets_without_mutation(tmp_path, targets):
    root, scene = _keep_project(tmp_path)
    before = load_project(root).model_dump()

    res = run_tool("set_keep", _ctx(root), {"targets": targets})

    assert res["ok"] is False
    assert res["needs_confirm"] is False
    assert res["payload"] == {}
    assert "targets" in res["message"]
    assert load_project(root).model_dump() == before
    assert current_scene(root, "s1")[0].constraints == scene.constraints


def test_set_keep_accepts_target_size_limits(tmp_path):
    root, _ = _keep_project(tmp_path)
    before = load_project(root).model_dump()
    targets = ["e1"] * 199 + ["x" * 200]

    res = run_tool("set_keep", _ctx(root), {"targets": targets})

    assert res["ok"] is True
    assert res["needs_confirm"] is True
    assert res["payload"]["keep_change"]["matched"] == 3
    assert load_project(root).model_dump() == before


def test_set_keep_preview_examples_are_first_five_matches(tmp_path):
    extra = [Constraint(pred=f"top(e{i},e2)") for i in range(3, 6)]
    root, scene = _keep_project(tmp_path, extra_constraints=extra)
    before = load_project(root).model_dump()

    res = run_tool("set_keep", _ctx(root), {"targets": ["e"], "on": False})

    assert res["ok"] is True
    assert res["needs_confirm"] is True
    preview = res["payload"]["keep_change"]
    assert preview["matched"] == 7
    assert preview["examples"] == [c.pred for c in scene.constraints[:5]]
    assert load_project(root).model_dump() == before


@pytest.mark.parametrize("preset", [[], {}, 1, True, None])
def test_set_keep_rejects_non_string_preset_without_mutation(tmp_path, preset):
    root, _ = _keep_project(tmp_path)
    before = load_project(root).model_dump()

    res = run_tool("set_keep", _ctx(root), {"preset": preset})

    assert res["ok"] is False
    assert res["needs_confirm"] is False
    assert res["payload"] == {}
    assert "preset" in res["message"]
    assert load_project(root).model_dump() == before


@pytest.mark.parametrize("preset", ["all", "none", "content_only", "motion_shape"])
def test_set_keep_preset_only_previews(tmp_path, preset):
    root, scene = _keep_project(tmp_path)
    before = load_project(root).model_dump()

    res = run_tool("set_keep", _ctx(root), {"preset": preset})

    assert res["ok"] is True
    assert res["needs_confirm"] is True
    assert res["payload"] == {"keep_change": {"preset": preset}}
    assert load_project(root).model_dump() == before
    assert current_scene(root, "s1")[0].constraints == scene.constraints


def test_set_keep_rejects_unknown_preset_without_mutation(tmp_path):
    root, _ = _keep_project(tmp_path)
    before = load_project(root).model_dump()
    res = run_tool("set_keep", _ctx(root), {"preset": "invalid"})
    assert res["ok"] is False
    assert res["needs_confirm"] is False
    assert load_project(root).model_dump() == before


@pytest.mark.parametrize("op", ["reassign", "mask", "bbox", "text"])
def test_correct_only_previews_and_never_submits_job(tmp_path, op):
    submit = Mock(return_value={"id": "job1"})
    args = {"element_id": "e1", "text": "Hello"}
    ctx = SessionContext(root=tmp_path, scene_id="s1", submit_job=submit)

    res = run_tool("correct", ctx, {"op": op, "args": args, "confirm": True})

    submit.assert_not_called()
    assert res["ok"] is True
    assert res["needs_confirm"] is True
    assert res["payload"] == {"correction": {"op": op, "args": args}}


def test_correct_preview_does_not_need_job_queue(tmp_path):
    res = run_tool("correct", _ctx(tmp_path), {"op": "text"})
    assert res["ok"] is True
    assert res["needs_confirm"] is True
    assert res["payload"] == {"correction": {"op": "text", "args": {}}}


@pytest.mark.parametrize("op", [None, "invalid"])
def test_correct_rejects_unknown_op_without_submitting(tmp_path, op):
    submit = Mock()
    res = run_tool("correct", SessionContext(tmp_path, "s1", submit_job=submit), {"op": op})
    submit.assert_not_called()
    assert res["ok"] is False
    assert res["needs_confirm"] is False


def test_correct_and_keep_descriptions_require_browser_confirmation():
    from keepframe.session.agent import SYSTEM
    from keepframe.session.tools import TOOL_SCHEMAS

    assert "correct·set_keep도 미리보기만 한다" in SYSTEM
    for tool in TOOL_SCHEMAS:
        if tool["function"]["name"] in ("correct", "set_keep"):
            assert "브라우저" in tool["function"]["description"]
            assert "미리보기" in tool["function"]["description"]


def test_set_keep_reports_no_match(tmp_path):
    root = tmp_path / "proj"
    scene = make_synthetic_scene(root / "scenes" / "s1", seed=2, with_text=False, frames=12)
    init_project(root, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, 11]}, scene.model_copy(update={"id": "s1"}))
    res = run_tool("set_keep", _ctx(root), {"targets": ["nope"], "on": True})
    assert res["ok"] is False


def test_edit_tool_preview_then_confirmed_edit_completes(tmp_path):
    root = tmp_path / "proj"
    sd = root / "scenes" / "s1"
    scene = make_synthetic_scene(sd, seed=4, with_text=True, frames=24)
    scene = scene.model_copy(update={"id": "s1"})
    scene.constraints = [c.model_copy(update={"keep": c.pred.startswith("type(")}) for c in extract_constraints(scene)]
    text = next(e for e in scene.elements if e.kind == "text")
    init_project(root, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, 23]}, scene)

    res = run_tool("edit", _ctx(root), {"prompt": "문구를 Hello로", "element": text.id, "confirm": True})
    assert res["ok"] is True
    assert res["needs_confirm"] is True
    assert current_scene(root, "s1")[1].id == "v1"
    done = edit(root, "s1", "문구를 Hello로", confirm=True, intent=res["payload"]["intent"])
    assert done.status == "done" and done.version.id == "v2"
    edited, _ = current_scene(root, "s1")
    assert edited.element(text.id).canonical.text == "Hello"


def test_edit_tool_needs_confirm(tmp_path):
    root = tmp_path / "proj"
    sd = root / "scenes" / "s1"
    scene = make_synthetic_scene(sd, seed=4, with_text=True, frames=24)
    scene = scene.model_copy(update={"id": "s1"})
    scene.constraints = [c.model_copy(update={"keep": c.pred.startswith("type(")}) for c in extract_constraints(scene)]
    text = next(e for e in scene.elements if e.kind == "text")
    init_project(root, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, 23]}, scene)

    res = run_tool("edit", _ctx(root), {"prompt": "문구를 Hello로", "element": text.id, "confirm": False})
    assert res["needs_confirm"] is True
    assert res["payload"]["intent"]


def test_edit_tool_attachment_preview(tmp_path):
    root = tmp_path / "proj"
    scene = make_synthetic_scene(root / "scenes" / "s1", seed=4, with_text=False, frames=12).model_copy(update={"id": "s1"})
    init_project(root, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, 11]}, scene)
    sprite = next(e for e in scene.elements if e.kind == "sprite")

    res = run_tool("edit", SessionContext(root, "s1", has_attachment=True), {"prompt": f"{sprite.id} 이미지 교체"})

    assert res["ok"] is True
    assert res["needs_confirm"] is True
    assert res["payload"]["intent"]["targets"] == [{"element": sprite.id, "property": "texture", "value": "attachment", "weight": None, "speed": None, "delay": None}]


def test_verify_and_report_tools(tmp_path):
    root = tmp_path / "proj"
    sd = root / "scenes" / "s1"
    scene = make_synthetic_scene(sd, seed=4, with_text=False, frames=12)
    scene = scene.model_copy(update={"id": "s1"})
    scene.constraints = [c.model_copy(update={"keep": True}) for c in extract_constraints(scene)]
    init_project(root, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, 11]}, scene)

    ver = run_tool("verify", _ctx(root), {})
    assert ver["ok"] is True
    assert "keep_pass_rate" in ver["payload"]["verify"]

    rep = run_tool("report", _ctx(root), {})
    assert rep["ok"] is True
    assert rep["payload"]["elements"] == [e.id for e in scene.elements]


def test_verify_tool_includes_counts_without_successful_predicates(tmp_path):
    root = tmp_path / "proj"
    sd = root / "scenes" / "s1"
    scene = make_synthetic_scene(sd, seed=4, with_text=False, frames=12)
    scene = scene.model_copy(update={"id": "s1"})
    scene.constraints = [c.model_copy(update={"keep": c.pred.startswith("type(")}) for c in extract_constraints(scene)]
    kept = sum(c.keep for c in scene.constraints)
    assert 0 < kept < len(scene.constraints)
    init_project(root, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, 11]}, scene)

    ver = run_tool("verify", _ctx(root), {})
    assert ver["ok"] is True
    payload = ver["payload"]["verify"]
    assert payload["keep_total"] == kept and payload["keep_failed"] == 0
    assert payload["keep_failures"] == [] and "keep_results" not in payload


def test_render_prepares_immutable_plan_without_submitting_job(tmp_path):
    root = tmp_path / "proj"
    scene = make_synthetic_scene(root / "scenes" / "s1", seed=4, with_text=False, frames=12)
    init_project(
        root,
        {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, 11]},
        scene.model_copy(update={"id": "s1"}),
    )
    prepared = []
    submitted = []
    ctx = SessionContext(
        root=root,
        scene_id="s1",
        prepare_render=lambda mode, backend, direction: prepared.append((mode, backend, direction))
        or {"id": "rp1", "backend": backend, "mode": mode},
        submit_job=lambda *args: submitted.append(args),
    )

    res = run_tool(
        "render",
        ctx,
        {"backend": "after_effects", "direction": "polish typography", "confirm": True},
    )

    assert res["ok"] is True
    assert res["needs_confirm"] is True
    assert res["payload"]["render_plan"] == {"id": "rp1", "backend": "after_effects", "mode": "preview"}
    assert prepared == [("preview", "after_effects", "polish typography")]
    assert submitted == []


def test_export_prepares_final_plan_and_requires_explicit_backend(tmp_path):
    prepared = []
    ctx = SessionContext(
        root=tmp_path,
        scene_id="s1",
        prepare_render=lambda mode, backend, direction: prepared.append((mode, backend, direction))
        or {"id": "rp2", "backend": backend, "mode": mode},
    )

    missing = run_tool("export", ctx, {})
    final = run_tool("export", ctx, {"backend": "native"})

    assert missing["ok"] is False
    assert final["needs_confirm"] is True
    assert final["payload"]["render_plan"]["mode"] == "final"
    assert prepared == [("final", "native", None)]
