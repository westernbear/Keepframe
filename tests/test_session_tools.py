import json

from keepframe.analyze.constraints import extract_constraints
from keepframe.ir.schema import Background, Constraint, Element, Canonical, Scene
from keepframe.ir.store import current_scene, init_project, scene_dir
from keepframe.ir.synth import make_synthetic_scene
from keepframe.session.tools import SessionContext, run_tool


def _ctx(root, scene_id="s1", version=None):
    return SessionContext(root=root, scene_id=scene_id, version=version)


def test_set_keep_toggles_matching_constraints(tmp_path):
    root = tmp_path / "proj"
    sd = root / "scenes" / "s1"
    scene = make_synthetic_scene(sd, seed=2, with_text=False, frames=12)
    scene = scene.model_copy(update={"id": "s1"})
    scene.constraints = extract_constraints(scene)
    init_project(root, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, 11]}, scene)

    target = scene.constraints[0].pred
    res = run_tool("set_keep", _ctx(root), {"targets": [target], "on": True})
    assert res["ok"] is True
    assert res["payload"]["version"]["id"] == "v2"
    updated, _ = current_scene(root, "s1")
    assert next(c for c in updated.constraints if c.pred == target).keep is True


def test_set_keep_reports_no_match(tmp_path):
    root = tmp_path / "proj"
    scene = make_synthetic_scene(root / "scenes" / "s1", seed=2, with_text=False, frames=12)
    init_project(root, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, 11]}, scene.model_copy(update={"id": "s1"}))
    res = run_tool("set_keep", _ctx(root), {"targets": ["nope"], "on": True})
    assert res["ok"] is False


def test_edit_tool_completes(tmp_path):
    root = tmp_path / "proj"
    sd = root / "scenes" / "s1"
    scene = make_synthetic_scene(sd, seed=4, with_text=True, frames=24)
    scene = scene.model_copy(update={"id": "s1"})
    scene.constraints = [c.model_copy(update={"keep": c.pred.startswith("type(")}) for c in extract_constraints(scene)]
    text = next(e for e in scene.elements if e.kind == "text")
    init_project(root, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, 23]}, scene)

    res = run_tool("edit", _ctx(root), {"prompt": "문구를 Hello로", "element": text.id, "confirm": True})
    assert res["ok"] is True
    assert res["needs_confirm"] is False
    assert res["payload"]["version"]["id"] == "v2"
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
