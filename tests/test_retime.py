import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from keepframe.analyze.constraints import apply_keep_preset, extract_constraints
from keepframe.edit.agent import TEMPORAL_MIN, edit
from keepframe.edit.apply import apply_edit
from keepframe.edit.intent import SCENE_LEVEL, Intent, Target, describe, plan
from keepframe.edit import retime
from keepframe.edit.retime import retime_element, retime_scene, retimed_range
from keepframe.ir.schema import Background, Canonical, Element, Keyframe, Scene, Track, UIComponent, UIModel, UIStateRange
from keepframe.ir.store import current_scene, init_project
from keepframe.ir.synth import make_synthetic_scene
from keepframe.session.tools import TOOL_SCHEMAS, SessionContext, run_tool
from keepframe.verify.verifier import VerifyReport, verify


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


@pytest.mark.parametrize("choices", [None, {}, {"timing_overflow": "keep"}])
def test_apply_overflow_needs_explicit_extension(tmp_path, choices):
    scene = _scene()
    before = scene.model_dump()
    with pytest.raises(ValueError, match="timing_overflow needs a choice"):
        apply_edit(scene, tmp_path, [Target(element="e1", property="timing", delay=0.2)], choices, None)
    assert scene.model_dump() == before


def _ui_scene():
    scene = _scene()
    scene.element("e1").kind = "ui"
    component = UIComponent(id="e1", kind="button", bbox=(0, 0, 10, 10), states=[
        UIStateRange(frames=(10, 19), state={"text": "Ready"}),
        UIStateRange(frames=(20, 29), state={"text": "Done"}),
    ])
    component.children = [component.model_copy(deep=True, update={"id": "child"})]
    scene.ui = UIModel(components=[component, component.model_copy(deep=True, update={"id": "e2", "children": []})])
    return scene


@pytest.mark.parametrize("confirm", [False, True])
def test_scene_timing_cap_fails_before_render(tmp_path, monkeypatch, confirm):
    scene = _scene()
    scene.frames = 3600
    scene.constraints = apply_keep_preset(extract_constraints(scene), "content_only")
    init_project(tmp_path, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size)}, scene)
    monkeypatch.setattr("keepframe.edit.agent.render", lambda *a, **k: pytest.fail("overlong scene reached render"))
    result = edit(tmp_path, "s1", "slow down", confirm=confirm,
                  intent={"targets": [{"property": "timing", "speed": 0.11}]})
    assert result.status == "failed" and result.error == "timing would make the scene longer than 120s"
    assert result.attempts == 0 and result.version is None
    assert current_scene(tmp_path, "s1")[0] == scene and current_scene(tmp_path, "s1")[1].id == "v1"


@pytest.mark.parametrize("targets, frames", [
    ([Target(property="timing", speed=0.5)] * 3, 600),
    ([Target(element="e1", property="timing", delay=1)], 3600),
    ([Target(element="e1", property="timing", speed=0.11)], 3600),
])
def test_timing_cap_covers_stacked_targets_and_element_extension(tmp_path, monkeypatch, targets, frames):
    scene = _scene()
    scene.frames = frames
    scene.element("e1").visible = (10, frames - 1)
    before = scene.model_dump()
    intent = Intent(targets=targets)
    with pytest.raises(ValueError, match="^timing would make the scene longer than 120s$"):
        plan(scene, intent)
    assert scene.model_dump() == before
    with pytest.raises(ValueError, match="^timing would make the scene longer than 120s$"):
        retime.apply_timing(scene.model_copy(deep=True), targets, {"timing_overflow": "extend_scene"})
    init_project(tmp_path, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size)}, scene)
    monkeypatch.setattr("keepframe.edit.agent.render", lambda *a, **k: pytest.fail("overlong scene reached render"))
    result = edit(tmp_path, "s1", "retime", confirm=True, intent=intent)
    assert result.status == "failed" and result.error == "timing would make the scene longer than 120s"
    assert result.attempts == 0 and current_scene(tmp_path, "s1")[1].id == "v1"


@pytest.mark.parametrize("fps", [30, 29.97])
def test_timing_cap_allows_exact_rounded_frame_limit(fps):
    assert retime.MAX_SCENE_SECONDS == 120
    scene = _scene()
    scene.fps, scene.frames = fps, math.ceil(120 * fps)
    targets = [Target(property="timing", speed=1)]
    retime.apply_timing(scene, targets, {})
    assert scene.frames == math.ceil(120 * fps)
    scene.frames += 1
    with pytest.raises(ValueError, match="^timing would make the scene longer than 120s$"):
        retime.apply_timing(scene, targets, {})


def test_timing_cap_applies_across_versions(tmp_path, monkeypatch):
    scene = _scene()
    scene.frames = 1800
    init_project(tmp_path, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size)}, scene)
    rendered = []
    monkeypatch.setattr("keepframe.edit.agent.compose", lambda *a, **k: None)
    monkeypatch.setattr("keepframe.edit.agent.render", lambda html, scene, dest: rendered.append(scene.frames))
    monkeypatch.setattr("keepframe.edit.agent.verify", lambda *a, **k: VerifyReport(schema_ok=True, passed=True, temporal=1))
    intent = {"targets": [{"property": "timing", "speed": 0.5}]}
    first = edit(tmp_path, "s1", "slow down", confirm=True, intent=intent)
    assert first.status == "done" and first.version.id == "v2"
    before = current_scene(tmp_path, "s1")
    second = edit(tmp_path, "s1", "slow down again", confirm=True, intent=intent)
    assert second.status == "failed" and second.error == "timing would make the scene longer than 120s"
    assert rendered == [3599] and current_scene(tmp_path, "s1") == before


@pytest.mark.parametrize("stage", ["apply_timing", "apply_edit"])
@pytest.mark.parametrize("message", ["timing would make the scene longer than 120s", "other timing error"])
def test_edit_catches_only_scene_length_errors(tmp_path, monkeypatch, stage, message):
    scene = _scene()
    init_project(tmp_path, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size)}, scene)

    def fail(*args, **kwargs):
        raise ValueError(message)

    monkeypatch.setattr(f"keepframe.edit.agent.{stage}", fail)
    monkeypatch.setattr("keepframe.edit.agent.render", lambda *a, **k: pytest.fail("failed timing reached render"))
    intent = {"targets": [{"property": "timing", "speed": 2}]}
    if message == "other timing error":
        with pytest.raises(ValueError, match=message):
            edit(tmp_path, "s1", "retime", confirm=True, intent=intent)
    else:
        result = edit(tmp_path, "s1", "retime", confirm=True, intent=intent)
        assert result.status == "failed" and result.error == message and result.attempts == 0
    assert current_scene(tmp_path, "s1")[1].id == "v1"


def test_session_rejects_seventeen_targets_at_tool_boundary(tmp_path, monkeypatch):
    scene = _scene()
    init_project(tmp_path, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size)}, scene)
    targets = [{"property": "timing", "speed": 1}] * 17
    monkeypatch.setattr("keepframe.edit.agent.edit", lambda *a, **k: pytest.fail("too many targets reached edit agent"))
    result = run_tool("edit", SessionContext(tmp_path, "s1"), {"prompt": "retime", "targets": targets, "confirm": True})
    assert not result["ok"] and not result["needs_choice"] and not result["needs_confirm"]
    assert result["message"].startswith("targets 형식 오류: targets:") and "16" in result["message"]
    assert current_scene(tmp_path, "s1")[1].id == "v1"
    assert len(Intent.model_validate({"targets": targets[:16]}).targets) == 16
    with pytest.raises(ValidationError):
        Intent.model_validate({"targets": targets})
    params = next(t for t in TOOL_SCHEMAS if t["function"]["name"] == "edit")["function"]["parameters"]
    assert params["properties"]["targets"]["maxItems"] == 16


@pytest.mark.parametrize("target, args", [
    (Target(property="timing", speed=0.5), (0.5, 0, 0)),
    (Target(element="e1", property="timing", delay=0.2), (1.0, 6, 10)),
    (Target(element="e1", property="timing", speed=2, delay=0.1), (2.0, 3, 10)),
])
def test_shared_timing_args(target, args):
    assert retime.timing_args(target, _el() if target.element else None, 30) == args


def test_plan_overflow_uses_prior_timing_targets(tmp_path):
    targets = [Target(property="timing", speed=2), Target(element="e1", property="timing", delay=0.2)]
    scene = _scene()
    built = plan(scene, Intent(targets=targets))
    conflict, = built.conflicts
    assert conflict.reason == "장면 끝(16프레임)을 넘어 21프레임까지 이어집니다."
    out = apply_edit(scene, tmp_path, built.items, {"timing_overflow": "extend_scene"}, None)
    assert out.frames == 21 and out.element("e1").visible == (11, 20)
    assert scene.frames == 30 and scene.element("e1").visible == (10, 29)


@pytest.mark.parametrize("speed, frames, ranges", [
    (2, 16, [(5, 10), (10, 14)]),
    (0.5, 59, [(20, 38), (40, 58)]),
])
def test_scene_timing_retimes_all_ui_states(tmp_path, speed, frames, ranges):
    scene = _ui_scene()
    before = scene.model_dump()
    out = apply_edit(scene, tmp_path, [Target(property="timing", speed=speed)], {}, None)
    assert out.frames == frames
    for component in [*out.ui.components, out.ui.components[0].children[0]]:
        assert [state.frames for state in component.states] == ranges
        assert [state.state for state in component.states] == [{"text": "Ready"}, {"text": "Done"}]
    assert scene.model_dump() == before


@pytest.mark.parametrize("speed, delay, frames, ranges", [
    (None, 0.2, 36, [(16, 25), (26, 35)]),
    (2, 0.1, 30, [(13, 17), (18, 23)]),
])
def test_element_timing_retimes_only_matching_ui_component(tmp_path, speed, delay, frames, ranges):
    scene = _ui_scene()
    out = apply_edit(scene, tmp_path, [Target(element="e1", property="timing", speed=speed, delay=delay)],
                     {"timing_overflow": "extend_scene"}, None)
    assert out.frames == frames and [state.frames for state in out.ui.components[0].states] == ranges
    assert out.ui.components[1] == scene.ui.components[1]
    assert out.ui.components[0].children == scene.ui.components[0].children


@pytest.mark.parametrize("target, frames, ranges", [
    (Target(property="timing", speed=2), 16, [(0, 10), (10, 15)]),
    (Target(element="e1", property="timing", delay=-0.2), 30, [(0, 13), (14, 29)]),
])
def test_timing_clamps_ui_ranges_to_resulting_scene(tmp_path, target, frames, ranges):
    scene = _ui_scene()
    scene.ui.components[0].states[0].frames = (0, 19)
    scene.ui.components[0].states[1].frames = (20, 40)
    out = apply_edit(scene, tmp_path, [target], {}, None)
    assert out.frames == frames and [state.frames for state in out.ui.components[0].states] == ranges


def test_edit_accepts_element_keyed_extension_and_retimes_ui_reference(tmp_path, monkeypatch):
    scene = _ui_scene()
    init_project(tmp_path, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size)}, scene)
    references = []

    def inspect_reference(edited, *args, **kwargs):
        references.append(kwargs["reference"])
        return verify(edited, *args, **kwargs)

    monkeypatch.setattr("keepframe.edit.agent.verify", inspect_reference)
    result = edit(tmp_path, "s1", "delay", confirm=True, choices={"e1": "extend_scene"},
                  intent={"targets": [{"element": "e1", "property": "timing", "delay": 0.2}]})
    assert result.status == "done" and result.verify.temporal >= TEMPORAL_MIN
    edited, version = current_scene(tmp_path, "s1")
    reference, = references
    assert version.id == "v2" and reference.frames == edited.frames == 36
    assert reference.ui == edited.ui
    assert [state.frames for state in edited.ui.components[0].states] == [(16, 25), (26, 35)]


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
    result = edit(tmp_path, "s1", args["prompt"], confirm=True, intent=preview["payload"]["intent"])
    assert result.status == "needs_choice" and result.plan.conflicts[-1].id == "keep_violation"
    after, version = current_scene(tmp_path, "s1")
    assert after == scene and version.id == "v1"


def test_session_element_delay_requests_extension_before_render(tmp_path, monkeypatch):
    scene = _scene()
    init_project(tmp_path, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size)}, scene)
    monkeypatch.setattr("keepframe.edit.agent.render", lambda *a, **k: pytest.fail("unresolved timing overflow reached render"))
    preview = run_tool("edit", SessionContext(tmp_path, "s1"), {"prompt": "요소를 늦춰줘", "confirm": True,
        "targets": [{"element": "e1", "property": "timing", "delay": 1.0}]})
    assert preview["needs_confirm"]
    result = edit(tmp_path, "s1", "요소를 늦춰줘", confirm=True, intent=preview["payload"]["intent"])
    assert result.status == "needs_choice" and result.plan.conflicts[0].choices == ["extend_scene"]
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


def _keyed_gradient_scene(ts=(0, 59)):
    from keepframe.ir.schema import Gradient, GradientKey, GradientStop
    flat = lambda c: Gradient(stops=[GradientStop(offset=0, color=c), GradientStop(offset=1, color=c)])
    colours = ["#ff0000", "#00ff00", "#0000ff"] if len(ts) == 3 else ["#ff0000", "#0000ff"]
    bg = Background(kind="gradient", value="#800080",
                    gradient_keys=[GradientKey(t=t, gradient=flat(c)) for t, c in zip(ts, colours)])
    return Scene(id="s1", size=(50, 50), fps=30, frames=60, background=bg, elements=[])


@pytest.mark.parametrize("speed, frames, keys", [(2.0, 31, [0, 30]), (0.5, 119, [0, 118])])
def test_scene_speed_remaps_gradient_keys(speed, frames, keys):
    """Final review: an animated gradient follows a scene speed edit; its last key still lands on the last frame."""
    from keepframe.ir.gradient import gradient_at
    scene = _keyed_gradient_scene()
    retime_scene(scene, speed)
    assert scene.frames == frames and [k.t for k in scene.background.gradient_keys] == keys
    assert gradient_at(scene.background, scene.frames - 1).stops[0].color == "#0000ff"
    assert gradient_at(scene.background, 0).stops[0].color == "#ff0000"
    Background.model_validate(scene.background.model_dump())               # still sorted and unique


def test_scene_speed_dedupes_collapsed_gradient_keys():
    scene = _keyed_gradient_scene((0, 1, 59))
    retime_scene(scene, 4.0)
    keys = scene.background.gradient_keys
    assert [k.t for k in keys] == [0, 15] and keys[0].gradient.stops[0].color == "#00ff00"   # the later key wins


def _video_el(eid="v1", visible=(10, 29)):
    return Element(id=eid, kind="sprite", visible=visible, canonical=Canonical(
        width=10, height=10, texture=f"assets/{eid}.png", video=f"assets/{eid}.video.webm"))


def test_scene_speed_sets_video_playback_rate():
    """Final review: a video background and a video sprite play at the edited speed; stills carry no rate."""
    scene = _scene()
    scene.background = Background(kind="video", value="assets/background.webm", poster="assets/background.png")
    scene.elements.append(_video_el())
    retime_scene(scene, 2.0)
    assert scene.background.video_rate == 2.0 and scene.element("v1").canonical.video_rate == 2.0
    assert scene.element("e1").canonical.video_rate == 1.0
    retime_scene(scene, 0.5)
    assert scene.background.video_rate == 1.0 and scene.element("v1").canonical.video_rate == 1.0
    dumped = scene.model_dump_json()
    assert "video_rate" not in dumped                                       # rate 1 never reaches the scene JSON


def test_element_speed_sets_only_that_video_rate():
    scene = _scene()
    scene.background = Background(kind="video", value="assets/background.webm")
    scene.elements.append(_video_el())
    apply_timing = retime.apply_timing
    apply_timing(scene, [Target(element="v1", property="timing", speed=2.0, delay=0.2)], None)
    v1 = scene.element("v1")
    assert v1.canonical.video_rate == 2.0 and v1.visible == (16, 26) and scene.background.video_rate == 1.0
    assert '"video_rate":2.0' in v1.model_dump_json() and "video_rate" not in scene.background.model_dump_json()
    still = _el()
    retime_element(still, 2.0, 0, 10)
    assert "video_rate" not in still.model_dump_json()
