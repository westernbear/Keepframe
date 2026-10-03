import pytest
from pydantic import ValidationError

from keepframe.edit.apply import apply_edit
from keepframe.edit.intent import Target, describe, interpret, plan
from keepframe.ir.schema import Background, Canonical, Element, Keyframe, Scene, Track
from keepframe.ir.store import current_scene, init_project
from keepframe.render.renderer import RenderResult
from keepframe.session.tools import SessionContext, run_tool


def _scene():
    return Scene(id="s1", size=(100, 50), fps=30, frames=5, background=Background(value="#000000"), elements=[])


def _scene_with_element():
    scene = _scene()
    scene.elements.append(Element(id="e1", kind="sprite", canonical=Canonical(width=20, height=10, color="#000000"),
                                  visible=(0, 4), tracks={"x": Track(keys=[Keyframe(t=0, v=10), Keyframe(t=4, v=30)])}))
    return scene


def test_interpret_background_color_name():
    intent = interpret("배경을 흰색으로 바꿔줘", _scene())
    assert [(t.element, t.property, t.value) for t in intent.targets] == [(None, "background", "#ffffff")]
    assert not intent.ambiguous
    assert intent.summary == "배경색을 #ffffff로 바꿉니다. 트랙은 유지합니다."


def test_apply_background(tmp_path):
    out = apply_edit(_scene(), tmp_path, [Target(property="background", value="#112233")], {}, None)
    assert (out.background.kind, out.background.value) == ("color", "#112233")


def test_background_rejects_element_and_names():
    with pytest.raises(ValidationError):
        Target(element="e1", property="background", value="#ffffff")
    with pytest.raises(ValidationError):
        Target(property="background", value="white")


@pytest.mark.parametrize("name, value", [
    ("흰색", "#ffffff"), ("하얀", "#ffffff"), ("WHITE", "#ffffff"),
    ("검정", "#000000"), ("검은", "#000000"), ("black", "#000000"),
    ("빨간", "#e53935"), ("빨강", "#e53935"), ("red", "#e53935"),
    ("파란", "#1e66f5"), ("파랑", "#1e66f5"), ("blue", "#1e66f5"),
    ("초록", "#2e7d32"), ("green", "#2e7d32"),
    ("노란", "#fdd835"), ("노랑", "#fdd835"), ("yellow", "#fdd835"),
    ("회색", "#9e9e9e"), ("gray", "#9e9e9e"),
    ("주황", "#fb8c00"), ("orange", "#fb8c00"),
    ("보라", "#8e24aa"), ("purple", "#8e24aa"),
])
@pytest.mark.parametrize("background", [False, True])
def test_interpret_color_names(name, value, background):
    prompt = f"set {'BACKGROUND' if background else 'color'} to {name}"
    intent = interpret(prompt, _scene_with_element(), element="e1")
    assert not intent.ambiguous
    assert [(t.element, t.property, t.value) for t in intent.targets] == [
        (None, "background", value) if background else ("e1", "color", value)
    ]


@pytest.mark.parametrize("prefix, element, property", [("background", None, "background"), ("color", "e1", "color")])
def test_interpret_hex_overrides_names_and_uses_last_hex(prefix, element, property):
    intent = interpret(f"{prefix} white #112233 #AbC", _scene_with_element())
    assert [(t.element, t.property, t.value) for t in intent.targets] == [(element, property, "#aabbcc")]


@pytest.mark.parametrize("value, expected", [("#AbC", "#aabbcc"), ("#A0b1C2", "#a0b1c2")])
def test_background_normalizes_hex(value, expected):
    assert Target(property="background", value=value).value == expected


@pytest.mark.parametrize("value", [None, "", "#12345", "#1234567", "#abc\n"])
def test_background_requires_full_hex(value):
    with pytest.raises(ValidationError, match="color value must be #rrggbb"):
        Target(property="background", value=value)


@pytest.mark.parametrize("element", ["", " \t\n"])
def test_background_normalizes_blank_element(element):
    assert Target(element=element, property="background", value="#112233").element is None


def test_apply_background_preserves_source_and_elements(tmp_path):
    scene = _scene_with_element()
    scene.background = Background(kind="image", value="assets/background.png", confidence=0.5)
    before = scene.model_dump()
    out = apply_edit(scene, tmp_path, [Target(property="background", value="#112233")], {}, None)
    assert out.background == Background(kind="color", value="#112233", confidence=1.0)
    assert scene.model_dump() == before
    assert out.model_dump(exclude={"background"}) == scene.model_dump(exclude={"background"})


def test_plan_keeps_background_without_an_element():
    scene = _scene()
    intent = interpret("background #112233", scene)
    built = plan(scene, intent)
    assert built.items == [Target(property="background", value="#112233")]
    assert not built.conflicts


def test_describe_background_never_prints_missing_value():
    assert "None" not in describe([Target.model_construct(property="background")])


def test_edit_tool_background_needs_confirm_then_creates_version(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    scene = _scene_with_element()
    init_project(root, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, 4]}, scene)
    monkeypatch.setattr("keepframe.edit.agent.render", lambda html, scene, out_dir: RenderResult(
        frames_dir=out_dir / "frames", frames=list(range(scene.frames)), hashes=[],
        bboxes={"e1": [[5 * f, -5, 20 + 5 * f, 5] for f in range(scene.frames)]}))
    ctx = SessionContext(root, "s1")
    args = {"prompt": "배경색 바꿔줘", "targets": [{"property": "background", "value": "#112233"}]}

    preview = run_tool("edit", ctx, args)
    assert preview["ok"] and preview["needs_confirm"]
    assert not preview["needs_choice"]
    assert preview["message"] == "배경색을 #112233로 바꿉니다. 트랙은 유지합니다."
    assert preview["payload"]["plan"]["items"][0]["property"] == "background"
    unchanged, parent = current_scene(root, "s1")
    assert parent.id == "v1" and unchanged.background.value == "#000000"

    done = run_tool("edit", ctx, {**args, "confirm": True})
    assert done["ok"] and not done["needs_confirm"], done
    assert done["payload"]["version"]["id"] == "v2"
    edited, version = current_scene(root, "s1")
    assert version.id == "v2" and version.parent == "v1"
    assert edited.background == Background(kind="color", value="#112233", confidence=1.0)
    assert edited.elements == scene.elements
    assert done["payload"]["verify"]["temporal"] == 1.0
