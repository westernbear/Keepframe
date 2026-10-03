import pytest

from keepframe.edit.agent import edit
from keepframe.ir.schema import Background, Canonical, Constraint, Element, Keyframe, Scene, Track
from keepframe.ir.store import current_scene, init_project, new_version


def _root(tmp_path):
    a = Element(id="e1", kind="sprite", canonical=Canonical(width=20, height=20), visible=(0, 19),
                tracks={"x": Track(keys=[Keyframe(t=0, v=20.0), Keyframe(t=10, v=120.0)])})
    b = Element(id="e2", kind="sprite", canonical=Canonical(width=20, height=20), visible=(0, 19),
                tracks={"x": Track(keys=[Keyframe(t=0, v=180.0)])})
    scene = Scene(id="s1", size=(200, 100), fps=30, frames=20, background=Background(), elements=[a, b],
                  constraints=[Constraint(pred="right(e2,e1)", keep=True)])
    init_project(tmp_path, {"file": "ref.mp4", "fps": 30, "size": [200, 100], "mode": "range", "range": [0, 19]}, scene)
    return tmp_path


def test_keep_violation_becomes_a_choice(tmp_path, monkeypatch):
    root = _root(tmp_path)
    monkeypatch.setattr("keepframe.edit.agent.apply_edit", lambda scene, *a, **k: _shifted(scene))
    res = edit(root, "s1", "x", confirm=True, intent={"targets": [{"element": "e1", "property": "color", "value": "#ffffff"}]})
    assert res.status == "needs_choice" and res.plan.conflicts[-1].id == "keep_violation"
    done = edit(root, "s1", "x", confirm=True, choices={"keep_violation": "release_keep"},
                intent={"targets": [{"element": "e1", "property": "color", "value": "#ffffff"}]})
    assert done.status == "done"
    scene, version = current_scene(root, "s1")
    assert not scene.constraints[0].keep and "keep 해제 1개" in version.note
    assert done.messages == [*done.verify.messages, "keep released: right(e2,e1)"]


@pytest.mark.parametrize("choices", [None, {"e1": "release_keep"}, {"keep_violation": "keep"}])
def test_keep_choice_blocks_render_and_leaves_version_unchanged(tmp_path, monkeypatch, choices):
    root = _root(tmp_path)
    before, version = current_scene(root, "s1")
    monkeypatch.setattr("keepframe.edit.agent.apply_edit", lambda scene, *a, **k: _shifted(scene))

    def unexpected(*a, **k):
        pytest.fail("unresolved keep conflict reached compose/render/verify")

    for name in ("compose", "render", "verify"):
        monkeypatch.setattr(f"keepframe.edit.agent.{name}", unexpected)
    res = edit(root, "s1", "x", confirm=True, choices=choices,
               intent={"targets": [{"element": "e1", "property": "color", "value": "#ffffff"}]})
    assert res.status == "needs_choice" and res.attempts == 0
    conflict = res.plan.conflicts[-1]
    assert conflict.id == "keep_violation" and conflict.element == "e1"
    assert conflict.choices == ["release_keep"]
    assert conflict.reason == "유지 조건 1개와 충돌: right(e2,e1)"
    after, current = current_scene(root, "s1")
    assert after == before and current == version


def test_release_only_violated_keeps_and_report_each_predicate(tmp_path, monkeypatch):
    root = _root(tmp_path)
    scene, _ = current_scene(root, "s1")
    scene.constraints.extend([
        Constraint(pred="right(e2,e1)@0", keep=True),
        Constraint(pred="intersect(e1,e1)", keep=True),
        Constraint(pred="left(e1,e2)", keep=False),
    ])
    parent = new_version(root, "s1", scene, note="additional constraints")
    monkeypatch.setattr("keepframe.edit.agent.apply_edit", lambda scene, *a, **k: _shifted(scene))
    done = edit(root, "s1", "x", confirm=True, choices={"keep_violation": "release_keep"},
                intent={"targets": [{"element": "e1", "property": "color", "value": "#ffffff"}]})
    assert done.status == "done" and done.attempts == 1
    scene, version = current_scene(root, "s1")
    assert [c.keep for c in scene.constraints] == [False, False, True, False]
    assert version.note == f"{done.summary} (keep 해제 2개)" and version.parent == parent.id
    assert done.verify.keep_results == [{"pred": "intersect(e1,e1)", "passed": True}]
    assert done.messages == [*done.verify.messages, "keep released: right(e2,e1)", "keep released: right(e2,e1)@0"]


def test_release_choice_without_violation_keeps_constraints(tmp_path):
    root = _root(tmp_path)
    done = edit(root, "s1", "x", confirm=True, choices={"keep_violation": "release_keep"},
                intent={"targets": [{"element": "e1", "property": "color", "value": "#ffffff"}]})
    assert done.status == "done"
    scene, version = current_scene(root, "s1")
    assert scene.constraints[0].keep and version.note == done.summary
    assert done.messages == done.verify.messages


def _shifted(scene):
    out = scene.model_copy(deep=True)
    out.element("e2").tracks["x"] = Track(keys=[Keyframe(t=0, v=0.0)])
    return out
