from keepframe.analyze.constraints import extract_constraints
from keepframe.edit.agent import ASSET_GEN_CAP, edit
from keepframe.edit.intent import interpret, plan
from keepframe.ir.schema import Canonical, Constraint, Element, FontGuess, Keyframe, Scene, Track, Background
from keepframe.ir.store import current_scene, init_project
from keepframe.ir.synth import make_synthetic_scene


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
