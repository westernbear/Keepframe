import pytest
from keepframe.analyze.constraints import apply_keep_preset, carry_keep, extract_constraints
from keepframe.analyze.pipeline import AnalyzeOptions, analyze, rerun
from keepframe.analyze.video import render_scene_video
from keepframe.ir.schema import Constraint
from keepframe.ir.store import current_scene, init_project, new_version
from keepframe.ir.synth import make_synthetic_scene
from keepframe.session.tools import SessionContext, run_tool
from keepframe.verify.verifier import verify

SPATIAL = ("left(", "right(", "top(", "bottom(", "intersect(")


def test_content_only_locks_motion_not_layout(tmp_path):
    scene = make_synthetic_scene(tmp_path, seed=7)
    cs = apply_keep_preset(extract_constraints(scene), "content_only")
    assert all(c.keep for c in cs if c.pred.startswith(("type(", "mag(", "dur(", "while(")))
    assert not any(c.keep for c in cs if c.pred.startswith(SPATIAL))


def test_unknown_preset_rejected():
    with pytest.raises(ValueError):
        apply_keep_preset([], "nope")


def test_carry_keep_preserves_user_choice():
    prev = [Constraint(pred="type(m_e1_1,'translation')", keep=False)]
    new = apply_keep_preset([Constraint(pred="type(m_e1_1,'translation')"), Constraint(pred="dur(m_e1_1,10)")], "content_only")
    assert [c.keep for c in carry_keep(new, prev)] == [False, True]


def test_analysis_turns_on_default_preset(tmp_path):
    gold = make_synthetic_scene(tmp_path / "gold", seed=3, with_text=False, frames=12)
    vid = render_scene_video(gold, tmp_path / "gold", tmp_path / "g.mp4")
    analyze(vid, 0, 11, tmp_path / "proj", AnalyzeOptions(ocr=False, refine=False))
    scene, _ = current_scene(tmp_path / "proj", "s1")
    assert any(c.keep for c in scene.constraints if c.pred.startswith("type("))


def test_set_keep_preset_tool(tmp_path):
    root = tmp_path / "proj"
    scene = make_synthetic_scene(root / "scenes" / "s1", seed=2, with_text=False, frames=12).model_copy(update={"id": "s1"})
    scene.constraints = extract_constraints(scene)
    init_project(root, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, 11]}, scene)
    res = run_tool("set_keep", SessionContext(root=root, scene_id="s1"), {"preset": "all"})
    assert res["ok"] is True
    updated, _ = current_scene(root, "s1")
    assert all(c.keep for c in updated.constraints)
    assert run_tool("set_keep", SessionContext(root=root, scene_id="s1"), {"preset": "nope"})["ok"] is False


def test_verify_warns_when_nothing_is_kept(tmp_path):
    scene = make_synthetic_scene(tmp_path, seed=2, with_text=False, frames=12)
    rep = verify(scene, tmp_path)
    assert any("no keep predicates" in m for m in rep.messages)


@pytest.mark.parametrize("preset, expected", [
    ("content_only", [True, True, True, True, True, True, True, False]),
    ("motion_shape", [True, True, False, False, True, True, False, False]),
    ("all", [True] * 8),
    ("none", [False] * 8),
])
def test_presets_replace_existing_flags_without_mutating_input(preset, expected):
    preds = ["type(m1,'translation')", "dir(m1,[1,0])", "mag(m1,10)", "dur(m1,5)",
             "before(m1,m2)", "after(m2,m1)", "while(m1,m3)", "left(e1,e2)"]
    original = [Constraint(pred=pred, keep=True) for pred in preds]
    updated = apply_keep_preset(original, preset)
    assert [c.keep for c in updated] == expected
    assert all(c.keep for c in original)


def test_rerun_preserves_choices_and_defaults_new_predicates(tmp_path):
    gold = make_synthetic_scene(tmp_path / "gold", seed=3, with_text=False, frames=12)
    vid = render_scene_video(gold, tmp_path / "gold", tmp_path / "g.mp4")
    root = tmp_path / "proj"
    analyze(vid, 0, 11, root, AnalyzeOptions(ocr=False, refine=False))
    scene, _ = current_scene(root, "s1")
    motion = next(c for c in scene.constraints if c.pred.startswith("type("))
    layout = next(c for c in scene.constraints if c.pred.startswith(SPATIAL))
    new_motion = next(c for c in scene.constraints if c.pred.startswith("dur("))
    motion.keep = False
    layout.keep = True
    scene.constraints.remove(new_motion)
    new_version(root, "s1", scene, note="manual keep choices", auto=False)
    rerun(root, "s1", "keyframes", note="preserve keep choices")
    updated, _ = current_scene(root, "s1")
    flags = {c.pred: c.keep for c in updated.constraints}
    assert flags[motion.pred] is False
    assert flags[layout.pred] is True
    assert flags[new_motion.pred] is True


def test_keep_api_applies_presets_then_individual_changes(tmp_path):
    from tests.test_web_review import _post
    from tests.test_web_server import start

    root = tmp_path / "ws" / "p1"
    scene = make_synthetic_scene(root / "scenes" / "s1", seed=2, with_text=False, frames=12).model_copy(update={"id": "s1"})
    scene.constraints = extract_constraints(scene)
    init_project(root, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, 11]}, scene)
    target = scene.constraints[0].pred
    srv = start(tmp_path / "ws")
    try:
        code, body = _post(srv, "/api/keep", {"project": "p1", "preset": "all"})
        assert code == 200
        updated, version = current_scene(root, "s1")
        assert version.id == body["version"]["id"]
        assert all(c.keep for c in updated.constraints)
        code, _ = _post(srv, "/api/keep", {"project": "p1", "preset": "none", "changes": [{"pred": target, "keep": True}]})
        assert code == 200
        updated, version = current_scene(root, "s1")
        assert [c.pred for c in updated.constraints if c.keep] == [target]
        code, body = _post(srv, "/api/keep", {"project": "p1", "preset": "nope"})
        assert code == 400 and body == {"error": "unknown preset"}
        assert current_scene(root, "s1")[1].id == version.id
    finally:
        srv.shutdown()
        srv.server_close()
