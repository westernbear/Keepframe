import pytest

from keepframe.analyze.constraints import extract_constraints
from keepframe.edit.agent import ASSET_GEN_CAP, TEMPORAL_MIN, edit
from keepframe.edit.apply import apply_edit
from keepframe.edit.intent import interpret, plan
from keepframe.ir.schema import Canonical, Constraint, Element, FontGuess, Keyframe, Scene, Track, Background
from keepframe.ir.store import current_scene, init_project
from keepframe.ir.synth import make_synthetic_scene, make_text_texture, make_texture
from keepframe.ir.tracks import element_bbox
from keepframe.render.renderer import RenderResult
from keepframe.verify.verifier import VerifyReport


def _scene_with_text(text="Hi", width=40):
    el = Element(
        id="e1",
        kind="text",
        role="text",
        canonical=Canonical(
            width=width,
            height=20,
            text=text,
            font=FontGuess(size_px=20),
            color="#ffffff",
        ),
        visible=(0, 9),
        tracks={"x": Track(keys=[Keyframe(t=0, v=10.0), Keyframe(t=9, v=80.0)])},
    )
    return Scene(
        id="s1",
        size=(200, 100),
        fps=30,
        frames=10,
        background=Background(),
        elements=[el],
        constraints=[Constraint(pred="type(m_e1,'translation')", keep=True)],
    )


def test_interpret_text_and_color():
    scene = _scene_with_text()
    text = interpret("문구를 Hello로", scene)
    assert text.targets[0].property == "text" and text.targets[0].value == "Hello"
    color = interpret("색을 #00ffaa로", scene, element="e1")
    assert color.targets[0].property == "color" and color.targets[0].value == "#00ffaa"


def test_plan_flags_text_overflow():
    scene = _scene_with_text(width=12)
    intent = interpret("문구를 HelloWorldOverflow로", scene)
    built = plan(scene, intent)
    assert built.conflicts and built.conflicts[0].id == "overflow"


def test_edit_keeps_tracks_and_passes_verify(tmp_path):
    root = tmp_path / "proj"
    sd = root / "scenes" / "s1"
    scene = make_synthetic_scene(sd, seed=4, with_text=True, frames=24)
    scene = scene.model_copy(update={"id": "s1"})
    scene.constraints = [
        c.model_copy(update={"keep": c.pred.startswith("type(")})
        for c in extract_constraints(scene)
    ]
    text = next(e for e in scene.elements if e.kind == "text")
    before = {k: [kf.model_dump() for kf in text.tracks[k].keys] for k in text.tracks}
    init_project(
        root,
        {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, scene.frames - 1]},
        scene,
    )
    preview = edit(root, "s1", "문구를 Hello로", element=text.id, confirm=False)
    assert preview.status == "needs_confirm"
    res = edit(root, "s1", "문구를 Hello로", element=text.id, confirm=True, intent=preview.intent)
    assert res.status == "done"
    assert res.version.id == "v2"
    assert res.verify.keep_pass_rate >= 0.95
    assert res.verify.temporal >= 0.7
    edited, _ = current_scene(root, "s1")
    got = edited.element(text.id)
    assert got.canonical.text == "Hello"
    after = {k: [kf.model_dump() for kf in got.tracks[k].keys] for k in got.tracks}
    assert after == before


@pytest.fixture
def fragmented_project(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    sd = root / "scenes" / "s1"
    scene = _scene_with_text(width=120)
    scene.frames = 24
    text = scene.element("e1")
    text.visible = (0, 23)
    text.tracks["x"].keys[-1].t = 23
    make_text_texture(sd / "assets" / "e1.png", "Hi", 20, (255, 255, 255))
    text.canonical.texture = "assets/e1.png"
    make_texture(sd / "assets" / "sprite.png", "rect", 10, 10, (255, 255, 255))
    scene.elements.extend(Element(id=f"fragment{i}", kind="sprite", visible=(i, i),
                                 canonical=Canonical(width=10, height=10, texture="assets/sprite.png"))
                          for i in range(20))
    scene.constraints = [c.model_copy(update={"keep": c.pred.startswith("type(")}) for c in extract_constraints(scene)]
    init_project(root, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size)}, scene)

    def render_probes(_html, edited, out_dir):
        frames = list(range(edited.frames))
        return RenderResult(frames_dir=out_dir / "frames", frames=frames, hashes=[],
                            bboxes={el.id: [list(element_bbox(el, f)) for f in frames] for el in edited.elements})

    monkeypatch.setattr("keepframe.edit.agent.render", render_probes)
    return root, scene


@pytest.mark.parametrize("prop, value", [("text", "Hello"), ("color", "#00ffaa"), ("background", "#112233")])
def test_text_edit_with_one_frame_sprites_keeps_tracks_and_passes(fragmented_project, prop, value):
    root, before = fragmented_project
    target = {"property": prop, "value": value}
    if prop != "background":
        target["element"] = "e1"
    result = edit(root, "s1", "내용을 바꿔줘", confirm=True, intent={"targets": [target]})
    assert result.verify.passed
    assert result.status == "done", f"temporal={result.verify.temporal}"
    assert result.verify.temporal == 1.0
    edited, version = current_scene(root, "s1")
    assert version.id == "v2"
    assert (edited.background.value if prop == "background" else getattr(edited.element("e1").canonical, prop)) == value
    assert [el.id for el in edited.elements] == [el.id for el in before.elements]
    assert [(el.visible, el.tracks) for el in edited.elements] == [(el.visible, el.tracks) for el in before.elements]


def test_timing_edit_with_unintended_track_change_names_temporal_gate(fragmented_project, monkeypatch):
    root, before = fragmented_project
    _, parent = current_scene(root, "s1")

    def unintended_reversal(scene, *args, **kwargs):
        out = apply_edit(scene, *args, **kwargs)
        keys = out.element("e1").tracks["x"].keys
        keys[0].v, keys[-1].v = keys[-1].v, keys[0].v
        return out

    monkeypatch.setattr("keepframe.edit.agent.apply_edit", unintended_reversal)
    result = edit(root, "s1", "속도를 바꿔줘", confirm=True,
                  intent={"targets": [{"element": "e1", "property": "timing", "speed": 2.0}]})
    assert result.status == "failed" and result.attempts == 1
    assert result.verify.passed and result.verify.temporal < TEMPORAL_MIN
    assert "시간 유사도" in result.error and "keep 검증" not in result.error
    assert result.messages == result.verify.messages
    assert current_scene(root, "s1") == (before, parent)
    assert parent.id == "v1"


@pytest.mark.parametrize("failure, expected", [
    ({"temporal": 0.63, "passed": True}, "시간 유사도 0.630 < 0.700"),
    ({"temporal": 0.6994, "passed": True}, "시간 유사도 0.699 < 0.700"),
    ({"keep_pass_rate": 0.25, "keep_results": [{"pred": "test", "passed": False}] * 3 + [{"pred": "ok", "passed": True}]},
     "keep 술어 3개 실패"),
    ({"keep_pass_rate": 0.0}, "keep 검증을 통과하지 못했습니다."),
    ({"layer_max_err_px": 4.2}, "레이어 위치 오차 4.2px"),
    ({"schema_ok": False}, "스키마/레이어 프로브 불완전"),
    ({"layer_probe_complete": False}, "스키마/레이어 프로브 불완전"),
    ({"temporal": 0.63, "keep_pass_rate": 0.0,
      "keep_results": [{"pred": "test", "passed": False}] * 3,
      "layer_max_err_px": 4.2, "schema_ok": False, "layer_probe_complete": False},
     "시간 유사도 0.630 < 0.700; keep 술어 3개 실패; 레이어 위치 오차 4.2px; 스키마/레이어 프로브 불완전"),
])
def test_edit_failure_names_each_gate_and_preserves_messages(fragmented_project, monkeypatch, failure, expected):
    root, _ = fragmented_project
    report = VerifyReport(**{"schema_ok": True, "keep_pass_rate": 1.0, "temporal": 1.0,
                             "layer_probe_complete": True, "passed": False,
                             "messages": ["original verification detail"], **failure})
    monkeypatch.setattr("keepframe.edit.agent.verify", lambda *args, **kwargs: report)
    result = edit(root, "s1", "문구를 Hello로", confirm=True,
                  intent={"targets": [{"element": "e1", "property": "text", "value": "Hello"}]})
    assert result.status == "failed" and result.verify == report and result.attempts == 1
    assert result.error == f"검증 실패: {expected}"
    assert result.messages == report.messages
    assert current_scene(root, "s1")[1].id == "v1"


def test_edit_does_not_accept_missing_layer_proof(monkeypatch):
    from keepframe.edit.agent import _passed
    from keepframe.verify.verifier import VerifyReport

    report = VerifyReport(schema_ok=True, keep_pass_rate=1.0, temporal=1.0, layer_probe_complete=False, passed=False)

    assert not _passed(report)

def test_edit_overflow_waits_for_choice(tmp_path):
    root = tmp_path / "proj"
    sd = root / "scenes" / "s1"
    scene = _scene_with_text("Hi", width=16)
    from keepframe.ir.synth import make_text_texture
    make_text_texture(sd / "assets" / "e1.png", "Hi", 20, (255, 255, 255))
    scene.element("e1").canonical.texture = "assets/e1.png"
    scene.constraints = [
        c.model_copy(update={"keep": c.pred.startswith("type(")})
        for c in extract_constraints(scene)
    ]
    init_project(
        root,
        {"file": "ref.mp4", "fps": 30, "size": [200, 100], "mode": "range", "range": [0, 9]},
        scene,
    )
    res = edit(root, "s1", "문구를 HelloWorldOverflow로", element="e1", confirm=True)
    assert res.status == "needs_choice"
    done = edit(
        root,
        "s1",
        "문구를 HelloWorldOverflow로",
        element="e1",
        confirm=True,
        intent=res.intent,
        choices={"overflow": "expand_box"},
    )
    assert done.status == "done"
    edited, _ = current_scene(root, "s1")
    assert edited.element("e1").canonical.text == "HelloWorldOverflow"


def test_asset_generation_cap_is_two():
    assert ASSET_GEN_CAP == 2


def test_edit_reports_actual_attempts_on_failure(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    sd = root / "scenes" / "s1"
    scene = _scene_with_text("Hi", width=16)
    from keepframe.ir.synth import make_text_texture
    make_text_texture(sd / "assets" / "e1.png", "Hi", 20, (255, 255, 255))
    scene.element("e1").canonical.texture = "assets/e1.png"
    scene.constraints = [
        c.model_copy(update={"keep": c.pred.startswith("type(")})
        for c in extract_constraints(scene)
    ]
    init_project(
        root,
        {"file": "ref.mp4", "fps": 30, "size": [200, 100], "mode": "range", "range": [0, 9]},
        scene,
    )
    intent = interpret("문구를 HelloWorldOverflow로", scene, element="e1")
    from keepframe.verify.verifier import VerifyReport

    def failing_verify(*args, **kwargs):
        return VerifyReport(schema_ok=True, keep_pass_rate=0.0, passed=False)

    monkeypatch.setattr("keepframe.edit.agent.verify", failing_verify)
    res = edit(
        root,
        "s1",
        "문구를 HelloWorldOverflow로",
        element="e1",
        confirm=True,
        intent=intent,
        choices={"e1": "expand_box"},
    )
    assert res.status == "failed"
    assert res.attempts == 1


def test_generated_assets_stop_at_two_distinct_candidates(tmp_path, monkeypatch):
    import cv2
    import numpy as np
    from keepframe.assets import AssetResponse
    from keepframe.verify.verifier import VerifyReport

    root = tmp_path / "proj"
    sd = root / "scenes" / "s1"
    scene = make_synthetic_scene(sd, seed=9, with_text=False, frames=6).model_copy(update={"id": "s1"})
    init_project(root, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size)}, scene)
    target = scene.elements[0].id
    calls = []

    class FakeAssets:
        def request(self, **kwargs):
            calls.append(kwargs)
            image = np.full((12, 12, 4), len(calls) * 40, np.uint8)
            ok, encoded = cv2.imencode(".png", image)
            assert ok
            return AssetResponse("image/png", encoded.tobytes())

    monkeypatch.setattr("keepframe.edit.agent.AssetClient", FakeAssets)
    monkeypatch.setattr("keepframe.edit.agent.compose", lambda _scene, directory, _out, **_: directory / "composition.html")
    monkeypatch.setattr("keepframe.edit.agent.render", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("keepframe.edit.agent.verify", lambda *_args, **_kwargs: VerifyReport(schema_ok=True, keep_pass_rate=0, layer_probe_complete=True, passed=False))

    preview = edit(root, "s1", "이미지를 새로 생성해", element=target)
    result = edit(root, "s1", "이미지를 새로 생성해", element=target, confirm=True, intent=preview.intent)
    assert result.status == "failed" and result.attempts == 2 and len(calls) == 2
    assert calls[1]["prompt"] != calls[0]["prompt"]


def test_generated_3d_asset_is_attached_as_glb(tmp_path, monkeypatch):
    from keepframe.assets import AssetResponse
    from keepframe.verify.verifier import VerifyReport
    from tests.test_three import _triangle_glb

    root = tmp_path / "proj"
    sd = root / "scenes" / "s1"
    scene = make_synthetic_scene(sd, seed=10, with_text=False, frames=6).model_copy(update={"id": "s1"})
    init_project(root, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size)}, scene)
    target = scene.elements[0].id

    class FakeAssets:
        def request(self, **kwargs):
            assert kwargs["kind"] == "3d"
            return AssetResponse("model/gltf-binary", _triangle_glb())

    monkeypatch.setattr("keepframe.edit.agent.AssetClient", FakeAssets)
    monkeypatch.setattr("keepframe.edit.agent.compose", lambda _scene, directory, _out, **_: directory / "composition.html")
    monkeypatch.setattr("keepframe.edit.agent.render", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("keepframe.edit.agent.verify", lambda *_args, **_kwargs: VerifyReport(schema_ok=True, keep_pass_rate=1, temporal=1, layer_probe_complete=True, passed=True))

    preview = edit(root, "s1", "3D 모델로 교체해", element=target)
    result = edit(root, "s1", "3D 모델로 교체해", element=target, confirm=True, intent=preview.intent)
    edited, _ = current_scene(root, "s1")
    assert result.status == "done"
    assert edited.element(target).kind == "3d"
    assert edited.element(target).canonical.model.endswith(".glb")
