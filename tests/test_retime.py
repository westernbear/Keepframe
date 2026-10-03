from pathlib import Path

import pytest
from pydantic import ValidationError

from keepframe.analyze.constraints import apply_keep_preset, extract_constraints
from keepframe.edit.agent import TEMPORAL_MIN, edit
from keepframe.edit.apply import apply_edit
from keepframe.edit.intent import SCENE_LEVEL, Intent, Target, describe, plan
from keepframe.edit.retime import retime_element, retime_scene, retimed_range
from keepframe.ir.schema import Background, Canonical, Element, Keyframe, Scene, Track
from keepframe.ir.store import current_scene, init_project
from keepframe.ir.synth import make_synthetic_scene
from keepframe.session.tools import TOOL_SCHEMAS, SessionContext, run_tool
from keepframe.verify.verifier import verify


def _el():
    return Element(id="e1", kind="sprite", canonical=Canonical(width=10, height=10), visible=(10, 29),
                   tracks={"x": Track(keys=[Keyframe(t=10, v=0.0), Keyframe(t=20, v=100.0), Keyframe(t=29, v=100.0)])})


def _scene():
    return Scene(id="s1", size=(50, 50), fps=30, frames=30, background=Background(), elements=[_el()])


def test_speed_up_halves_key_offsets_from_entrance():
    el = _el(); retime_element(el, speed=2.0, delay_frames=0, anchor=10)
    assert [k.t for k in el.tracks["x"].keys] == [10, 15, 20] and el.visible == (10, 20)


def test_delay_shifts_everything():
    el = _el(); retime_element(el, speed=1.0, delay_frames=6, anchor=10)
    assert el.visible == (16, 35) and el.tracks["x"].keys[0].t == 16


def test_retime_dedupes_collapsed_keys():
    el = _el(); retime_element(el, speed=10.0, delay_frames=0, anchor=10)
    ts = [k.t for k in el.tracks["x"].keys]
    assert ts == sorted(set(ts))


def test_scene_speed_shrinks_frames():
    scene = _scene()
    retime_scene(scene, 3.0)
    assert scene.frames == 11 and scene.element("e1").visible[1] <= scene.frames - 1


def test_overflow_and_validation():
    scene = _scene()
    built = plan(scene, Intent(targets=[Target(element="e1", property="timing", delay=1.0)]))
    assert any(c.id == "timing_overflow" for c in built.conflicts)
    with pytest.raises(ValidationError):
        Target(element="e1", property="timing")
    with pytest.raises(ValidationError):
        Target(property="timing", delay=0.5)


def test_retimed_range_does_not_mutate_element():
    el = _el()
    before = el.model_dump()
    assert retimed_range(el, 2.0, 6, 10) == (16, 26)
    assert el.model_dump() == before


def test_retime_preserves_values_easing_canonical_z_and_raw():
    el = _el()
    el.raw = "raw/e1.npz"
    el.tracks["x"].keys[0].ease = (0.25, 0.1, 0.25, 1.0)
    el.tracks["opacity"] = Track(keys=[Keyframe(t=10, v=0), Keyframe(t=29, v=1)])
    before = el.model_copy(deep=True)
    retime_element(el, speed=2.0, delay_frames=3, anchor=10)
    assert el.visible == (13, 23)
    assert [k.t for k in el.tracks["opacity"].keys] == [13, 23]
    assert [(k.v, k.ease) for k in el.tracks["x"].keys] == [(k.v, k.ease) for k in before.tracks["x"].keys]
    assert (el.canonical, el.z, el.raw) == (before.canonical, before.z, before.raw)


def test_collapsed_keys_keep_later_value_and_easing():
    el = _el()
    el.tracks["x"] = Track(keys=[Keyframe(t=10, v=0), Keyframe(t=11, v=1, ease=(0, 0, 1, 1)), Keyframe(t=29, v=2)])
    retime_element(el, speed=10.0, delay_frames=0, anchor=10)
    assert [(k.t, k.v, k.ease) for k in el.tracks["x"].keys] == [(10, 1, (0, 0, 1, 1)), (12, 2, None)]


def test_negative_delay_before_start_fails_without_mutation():
    el = _el()
    before = el.model_dump()
    with pytest.raises(ValueError, match="timing moves e1 before the scene start"):
        retime_element(el, speed=1.0, delay_frames=-11, anchor=10)
    assert el.model_dump() == before


def test_negative_delay_within_scene_is_allowed():
    el = _el()
    retime_element(el, speed=1.0, delay_frames=-10, anchor=10)
    assert el.visible == (0, 19)
    assert [k.t for k in el.tracks["x"].keys] == [0, 10, 19]


@pytest.mark.parametrize("speed, frames, visible", [(0.5, 59, (20, 58)), (1.0, 30, (10, 29)), (3.0, 11, (3, 10))])
def test_scene_speed_anchors_all_elements_at_zero(speed, frames, visible):
    scene = _scene()
    scene.elements.append(_el().model_copy(deep=True, update={"id": "e2", "visible": (0, 29)}))
    retime_scene(scene, speed)
    assert scene.frames == frames and scene.fps == 30
    assert scene.element("e1").visible == visible
    assert scene.element("e2").visible == (0, visible[1])


@pytest.mark.parametrize("fields", [{"speed": 0.1}, {"speed": 10.1}, {"delay": -30.1}, {"delay": 30.1}])
def test_timing_target_keeps_existing_bounds(fields):
    with pytest.raises(ValidationError):
        Target(element="e1", property="timing", **fields)


def test_scene_timing_is_in_plan_and_tool_schema():
    target = Target(property="timing", speed=1.5)
    assert SCENE_LEVEL == frozenset({"background", "timing"})
    built = plan(_scene(), Intent(targets=[target]))
    assert built.items == [target] and not built.conflicts
    params = next(t for t in TOOL_SCHEMAS if t["function"]["name"] == "edit")["function"]["parameters"]
    assert "timing" in params["properties"]["targets"]["items"]["properties"]["property"]["enum"]


def test_timing_overflow_has_exact_choice_and_reason():
    scene = _scene()
    built = plan(scene, Intent(targets=[Target(element="e1", property="timing", delay=1.0)]))
    assert len(built.conflicts) == 1
    assert built.conflicts[0].model_dump() == {
        "id": "timing_overflow", "element": "e1", "choices": ["extend_scene"],
        "reason": "장면 끝(30프레임)을 넘어 60프레임까지 이어집니다.",
    }
    assert not plan(scene, Intent(targets=[Target(element="e1", property="timing", speed=2.0)])).conflicts


@pytest.mark.parametrize("choices", [None, {}, {"timing_overflow": "keep"}, {"e1": "extend_scene"}])
def test_apply_overflow_needs_explicit_extension(tmp_path, choices):
    scene = _scene()
    before = scene.model_dump()
    with pytest.raises(ValueError, match="timing_overflow needs a choice"):
        apply_edit(scene, tmp_path, [Target(element="e1", property="timing", delay=0.2)], choices, None)
    assert scene.model_dump() == before


def test_apply_element_timing_extends_scene_on_choice_and_preserves_source(tmp_path):
    scene = _scene()
    before = scene.model_dump()
    out = apply_edit(scene, tmp_path, [Target(element="e1", property="timing", delay=0.2)], {"timing_overflow": "extend_scene"}, None)
    assert out.frames == 36 and out.element("e1").visible == (16, 35)
    assert out.element("e1").provenance == "manual"
    assert scene.model_dump() == before


def test_apply_combines_element_speed_and_delay_in_seconds(tmp_path):
    out = apply_edit(_scene(), tmp_path, [Target(element="e1", property="timing", speed=2.0, delay=0.1)], {}, None)
    assert out.frames == 30 and out.element("e1").visible == (13, 23)
    assert [k.t for k in out.element("e1").tracks["x"].keys] == [13, 18, 23]


def test_apply_scene_timing_preserves_source(tmp_path):
    scene = _scene()
    before = scene.model_dump()
    out = apply_edit(scene, tmp_path, [Target(property="timing", speed=3.0)], {}, None)
    assert out.frames == 11 and out.element("e1").visible == (3, 10)
    assert scene.model_dump() == before


@pytest.mark.parametrize("element,speed,delay,phrase", [
    ("e1", 2.0, 0.5, "e1 속도 ×2 지연 0.5s로"),
    (None, 1.5, None, "장면 전체 속도 ×1.5 지연 0s로"),
    ("e1", None, -0.2, "e1 속도 ×1 지연 -0.2s로"),
])
def test_describe_timing(element, speed, delay, phrase):
    assert describe([Target(element=element, property="timing", speed=speed, delay=delay)]) == phrase + " 바꿉니다. 트랙은 유지합니다."


def test_extend_scene_choice_has_exact_korean_and_english_copy():
    src = Path("keepframe/web/static/js/i18n.js").read_text()
    assert '"review.editChoice.extend_scene": "장면 길이 늘리기"' in src.split("en: {", 1)[0]
    assert '"review.editChoice.extend_scene": "Extend the scene"' in src.split("en: {", 1)[1]


def test_session_scene_speed_requests_keep_choice_before_render(tmp_path, monkeypatch):
    scene = _scene()
    scene.constraints = apply_keep_preset(extract_constraints(scene), "content_only")
    init_project(tmp_path, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size)}, scene)

    def unexpected(*args, **kwargs):
        pytest.fail("unresolved keep conflict reached render")

    monkeypatch.setattr("keepframe.edit.agent.render", unexpected)
    ctx = SessionContext(tmp_path, "s1")
    args = {"prompt": "장면 속도를 바꿔줘", "targets": [{"element": None, "property": "timing", "speed": 1.5}]}
    preview = run_tool("edit", ctx, args)
    assert preview["needs_confirm"] and not preview["needs_choice"]
    result = run_tool("edit", ctx, {**args, "confirm": True})
    assert result["needs_choice"] and result["payload"]["plan"]["conflicts"][-1]["id"] == "keep_violation"
    after, version = current_scene(tmp_path, "s1")
    assert after == scene and version.id == "v1"


def test_session_element_delay_requests_extension_before_render(tmp_path, monkeypatch):
    scene = _scene()
    init_project(tmp_path, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size)}, scene)
    monkeypatch.setattr("keepframe.edit.agent.render", lambda *a, **k: pytest.fail("unresolved timing overflow reached render"))
    result = run_tool("edit", SessionContext(tmp_path, "s1"), {"prompt": "요소를 늦춰줘", "confirm": True,
        "targets": [{"element": "e1", "property": "timing", "delay": 1.0}]})
    assert result["needs_choice"] and result["payload"]["plan"]["conflicts"][0]["choices"] == ["extend_scene"]
    assert current_scene(tmp_path, "s1")[1].id == "v1"


def test_scene_speed_release_keep_finishes_with_verified_retiming(tmp_path):
    scene = make_synthetic_scene(tmp_path / "scenes" / "s1", seed=4, frames=24, with_text=False).model_copy(update={"id": "s1"})
    scene.constraints = apply_keep_preset(extract_constraints(scene), "content_only")
    init_project(tmp_path, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size)}, scene)
    intent = {"targets": [{"element": None, "property": "timing", "speed": 1.5}]}
    blocked = edit(tmp_path, "s1", "장면 속도를 바꿔줘", confirm=True, intent=intent)
    assert blocked.status == "needs_choice" and blocked.plan.conflicts[-1].id == "keep_violation"
    assert blocked.attempts == 0 and current_scene(tmp_path, "s1")[1].id == "v1"
    done = edit(tmp_path, "s1", "장면 속도를 바꿔줘", confirm=True, intent=intent, choices={"keep_violation": "release_keep"})
    assert done.status == "done" and done.version.id == "v2"
    assert TEMPORAL_MIN == 0.7 and done.verify.temporal >= TEMPORAL_MIN
    edited, version = current_scene(tmp_path, "s1")
    assert version.id == "v2" and edited.frames == 17 < scene.frames
    assert edited.fps == scene.fps


def test_retiming_with_unintended_unrelated_track_change_fails_temporal_gate(tmp_path, monkeypatch):
    scene = _scene()
    scene.elements.append(Element(id="e2", kind="sprite", canonical=Canonical(width=10, height=10), visible=(0, 29),
                                  tracks={"x": Track(keys=[Keyframe(t=0, v=100), Keyframe(t=29, v=10)])}))
    init_project(tmp_path, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size)}, scene)
    before, parent = current_scene(tmp_path, "s1")

    def unintended_shift(scene, *args, **kwargs):
        out = apply_edit(scene, *args, **kwargs)
        out.element("e2").tracks["x"].keys[-1].v += 1000
        return out

    monkeypatch.setattr("keepframe.edit.agent.apply_edit", unintended_shift)
    result = edit(tmp_path, "s1", "요소 속도를 바꿔줘", confirm=True,
                  intent={"targets": [{"element": "e1", "property": "timing", "speed": 2.0}]})
    assert result.status == "failed" and result.attempts == 1
    assert result.verify.passed and result.verify.temporal < TEMPORAL_MIN
    assert current_scene(tmp_path, "s1") == (before, parent)


@pytest.mark.parametrize("speed, delay, visible", [(None, 0.2, (16, 35)), (2.0, 0.5, (25, 35))])
def test_element_timing_reference_extends_scene_and_excludes_content_edits(tmp_path, monkeypatch, speed, delay, visible):
    scene = _scene()
    init_project(tmp_path, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size)}, scene)
    before = scene.model_dump()
    references = []

    def inspect_reference(edited, *args, **kwargs):
        references.append(kwargs["reference"])
        return verify(edited, *args, **kwargs)

    monkeypatch.setattr("keepframe.edit.agent.verify", inspect_reference)
    result = edit(tmp_path, "s1", "요소 타이밍과 색을 바꿔줘", confirm=True,
                  choices={"timing_overflow": "extend_scene"}, intent={"targets": [
                      {"element": "e1", "property": "timing", "speed": speed, "delay": delay},
                      {"element": "e1", "property": "color", "value": "#ff0000"},
                      {"property": "background", "value": "#ffffff"},
                  ]})
    assert result.status == "done" and result.verify.temporal >= TEMPORAL_MIN
    reference, = references
    edited, _ = current_scene(tmp_path, "s1")
    assert reference.frames == edited.frames == 36
    assert reference.element("e1").visible == edited.element("e1").visible == visible
    assert reference.element("e1").tracks == edited.element("e1").tracks
    assert reference.background == scene.background and reference.element("e1").canonical == scene.element("e1").canonical
    assert edited.background.value == "#ffffff" and edited.element("e1").canonical.color == "#ff0000"
    assert scene.model_dump() == before
