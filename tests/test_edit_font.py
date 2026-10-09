from pathlib import Path

import pytest
from pydantic import ValidationError

from keepframe.edit.agent import edit
from keepframe.edit.apply import apply_edit
from keepframe.edit.intent import Intent, Target, describe, plan
from keepframe.ir.schema import Background, Canonical, Element, FontGuess, Scene
from keepframe.ir.store import current_scene, init_project
from keepframe.render.renderer import RenderResult
from keepframe.session.tools import SessionContext, run_tool


def _scene():
    el = Element(id="e1", kind="text", canonical=Canonical(width=200, height=40, text="Sale", color="#ffffff",
                 font=FontGuess(family_guess="sans-serif", weight=400, size_px=32)), visible=(0, 4))
    return Scene(id="s1", size=(300, 100), fps=30, frames=5, background=Background(), elements=[el])


@pytest.mark.parametrize("value", ["x;background:url(evil)", "x'", 'x"', "x\\", "x:charset=ac00", "x\ny", "x" * 65])
def test_font_value_cannot_inject_css(value):
    with pytest.raises(ValidationError, match="font family may only contain letters, digits, spaces and hyphens"):
        Target(element="e1", property="font", value=value)


@pytest.mark.parametrize("value", ["DejaVu Serif", "Noto Sans CJK KR", "맑은 고딕", "sans-serif", "Font-123", "x" * 64])
def test_font_accepts_safe_family_names(value):
    assert Target(element="e1", property="font", value=value).value == value


@pytest.mark.parametrize("value", [None, "", "   "])
def test_font_needs_family_or_weight(value):
    with pytest.raises(ValidationError, match="font needs a family or weight"):
        Target(element="e1", property="font", value=value)


def test_font_strips_family_and_normalizes_blank_weight_only_family():
    assert Target(property="font", value="  DejaVu Serif  ").value == "DejaVu Serif"
    assert Target(property="font", value="   ", weight=700).value is None


@pytest.mark.parametrize("weight", [99, 901])
def test_font_weight_remains_bounded(weight):
    with pytest.raises(ValidationError):
        Target(element="e1", property="font", weight=weight)


def test_apply_font_family_and_weight(tmp_path):
    scene = _scene()
    before = scene.model_dump()
    out = apply_edit(scene, tmp_path, [Target(element="e1", property="font", value="DejaVu Serif", weight=700)], {}, None)
    font = out.element("e1").canonical.font
    assert (font.family_guess, font.weight, font.size_px) == ("DejaVu Serif", 700, 32)
    assert out.element("e1").canonical.texture.startswith("assets/e1.txt")
    assert (tmp_path / out.element("e1").canonical.texture).is_file()
    assert out.element("e1").canonical.text == "Sale"
    assert out.element("e1").provenance == "manual"
    assert out.element("e1").tracks == scene.element("e1").tracks
    assert scene.model_dump() == before


@pytest.mark.parametrize("family,weight,expected", [("DejaVu Serif", None, ("DejaVu Serif", 400)), (None, 700, ("sans-serif", 700))])
def test_apply_partial_font_edit_preserves_other_fields(tmp_path, family, weight, expected):
    out = apply_edit(_scene(), tmp_path, [Target(element="e1", property="font", value=family, weight=weight)], {}, None)
    font = out.element("e1").canonical.font
    assert (font.family_guess, font.weight) == expected


def test_apply_font_without_existing_guess_uses_defaults(tmp_path):
    scene = _scene()
    scene.element("e1").canonical.font = None
    out = apply_edit(scene, tmp_path, [Target(element="e1", property="font", weight=700)], {}, None)
    assert out.element("e1").canonical.font == FontGuess(weight=700)


@pytest.mark.parametrize("kind,text", [("sprite", "Sale"), ("text", None), ("text", "")])
def test_apply_font_rejects_nontext_elements(tmp_path, kind, text):
    scene = _scene()
    scene.element("e1").kind = kind
    scene.element("e1").canonical.text = text
    with pytest.raises(ValueError, match="font edit needs a text element"):
        apply_edit(scene, tmp_path, [Target(element="e1", property="font", weight=700)], {}, None)
    assert not (tmp_path / "assets").exists()


def test_missing_font_is_a_conflict(monkeypatch):
    monkeypatch.setattr("keepframe.edit.intent.resolve_families", lambda name: ("DejaVu Sans",))
    built = plan(_scene(), Intent(targets=[Target(element="e1", property="font", value="Brand Sans")]))
    conflict = next(c for c in built.conflicts if c.id == "font_missing")
    assert conflict.element == "e1" and conflict.choices == ["use_fallback"]
    assert conflict.reason == "Brand Sans 폰트가 설치되어 있지 않습니다. DejaVu Sans(으)로 그려집니다."


@pytest.mark.parametrize("family,names,missing", [
    ("dejavu serif", ("DejaVu Serif",), False),
    ("나눔고딕", ("NanumGothic", "나눔고딕"), False),
    ("Brand Sans", ("DejaVu Sans", "다른 폰트"), True),
])
def test_font_installation_matches_any_family_name_case_insensitively(monkeypatch, family, names, missing):
    from keepframe.edit import textraster

    def fc(fmt, name, hangul=False):
        assert name == family
        return ",".join(names) if fmt == "%{family}" else names[0]

    monkeypatch.setattr(textraster, "_fc", fc)
    monkeypatch.setattr("keepframe.edit.apply.measure_text", lambda *args: (100, 40))
    textraster.resolve_family.cache_clear()
    try:
        built = plan(_scene(), Intent(targets=[Target(element="e1", property="font", value=family)]))
        conflicts = [c for c in built.conflicts if c.id == "font_missing"]
        assert bool(conflicts) == missing
        if missing:
            assert conflicts[0].choices == ["use_fallback"]
            assert conflicts[0].reason == f"{family} 폰트가 설치되어 있지 않습니다. {names[0]}(으)로 그려집니다."
    finally:
        textraster.resolve_family.cache_clear()


@pytest.mark.parametrize("resolved", [("DejaVu Serif",), ()])
def test_installed_font_or_unavailable_fontconfig_has_no_missing_conflict(monkeypatch, resolved):
    monkeypatch.setattr("keepframe.edit.intent.resolve_families", lambda name: resolved)
    built = plan(_scene(), Intent(targets=[Target(element="e1", property="font", value="DejaVu Serif")]))
    assert not any(c.id == "font_missing" for c in built.conflicts)


@pytest.mark.parametrize("width,overflow", [(229, False), (231, True)])
def test_font_plan_measures_new_family_and_flags_overflow(monkeypatch, width, overflow):
    measured = []
    monkeypatch.setattr("keepframe.edit.intent.resolve_families", lambda name: (name,))

    def measure(text, size, family):
        measured.append((text, size, family))
        return width, 40

    monkeypatch.setattr("keepframe.edit.apply.measure_text", measure)
    built = plan(_scene(), Intent(targets=[Target(element="e1", property="font", value="DejaVu Serif")]))
    assert measured == [("Sale", 32, "DejaVu Serif")]
    assert bool(built.conflicts) == overflow
    if overflow:
        assert built.conflicts[0].id == "overflow"
        assert built.conflicts[0].choices == ["shrink_font", "wrap", "expand_box"]
        assert built.conflicts[0].reason == "바꾼 폰트로는 문구가 원래 상자보다 깁니다."


@pytest.mark.parametrize("has_font", [False, True])
def test_weight_only_plan_uses_current_or_default_family(monkeypatch, has_font):
    scene = _scene()
    scene.element("e1").canonical.font = FontGuess(family_guess="DejaVu Serif", size_px=24) if has_font else None
    measured = []
    monkeypatch.setattr("keepframe.edit.intent.resolve_families", lambda _: pytest.fail("weight-only edit must not check installation"))
    monkeypatch.setattr("keepframe.edit.apply.measure_text", lambda text, size, family: measured.append((text, size, family)) or (100, 40))
    assert not plan(scene, Intent(targets=[Target(element="e1", property="font", weight=700)])).conflicts
    assert measured == [("Sale", 24 if has_font else 32, "DejaVu Serif" if has_font else "sans-serif")]


@pytest.mark.parametrize("choice", ["shrink_font", "wrap", "expand_box"])
def test_font_edit_reuses_overflow_choices(tmp_path, monkeypatch, choice):
    from keepframe.edit import apply

    scene = _scene()
    scene.element("e1").canonical.width = 60
    measured, rendered = [], []
    real_write = apply.write_text_texture

    def measure(text, size, family):
        measured.append(family)
        return (80 if size > 28 else 50), 40

    def write(path, text, size, color, lines=None, family="sans-serif"):
        rendered.append((text, size, lines, family))
        return real_write(path, text, size, color, lines=lines, family=family)

    monkeypatch.setattr(apply, "measure_text", measure)
    monkeypatch.setattr(apply, "write_text_texture", write)
    out = apply_edit(scene, tmp_path, [Target(element="e1", property="font", value="DejaVu Serif", weight=700)], {"overflow": choice}, None)
    assert out.element("e1").canonical.font.weight == 700
    assert rendered == [("Sale", 28 if choice == "shrink_font" else 32, ["Sa", "le"] if choice == "wrap" else None, "DejaVu Serif")]
    if choice == "shrink_font":
        assert measured and set(measured) == {"DejaVu Serif"}


@pytest.mark.parametrize("family,weight,part", [("DejaVu Serif", 700, "DejaVu Serif 700"), (None, 700, " 700"), ("DejaVu Serif", None, "DejaVu Serif")])
def test_describe_font(family, weight, part):
    assert describe([Target(element="e1", property="font", value=family, weight=weight)]) == f"e1 폰트를 {part}로 바꿉니다. 트랙은 유지합니다."


def test_font_missing_waits_for_explicit_fallback_then_creates_version(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    scene = _scene()
    init_project(root, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size)}, scene)
    monkeypatch.setattr("keepframe.edit.intent.resolve_families", lambda _: ("DejaVu Sans",))
    monkeypatch.setattr("keepframe.edit.agent.render", lambda html, scene, out_dir: RenderResult(
        frames_dir=out_dir / "frames", frames=list(range(scene.frames)), hashes=[],
        bboxes={"e1": [[-100, -20, 100, 20]] * scene.frames}))
    ctx = SessionContext(root, "s1")
    args = {"prompt": "폰트를 바꿔줘", "targets": [{"element": "e1", "property": "font", "value": "Brand Sans", "weight": 700}]}
    preview = run_tool("edit", ctx, args)
    assert preview["ok"] and preview["needs_confirm"]
    model_choice = run_tool("edit", ctx, {**args, "confirm": True, "choices": {"font_missing": "use_fallback"}})
    assert model_choice["ok"] and model_choice["needs_confirm"] and not model_choice["needs_choice"]
    assert model_choice["payload"] == preview["payload"]
    unchanged, version = current_scene(root, "s1")
    assert version.id == "v1" and unchanged == scene
    blocked = edit(root, "s1", args["prompt"], confirm=True, intent=preview["payload"]["intent"])
    assert blocked.status == "needs_choice" and blocked.plan.conflicts[0].id == "font_missing"
    assert current_scene(root, "s1")[1].id == "v1"
    done = edit(root, "s1", args["prompt"], confirm=True, intent=preview["payload"]["intent"], choices={"font_missing": "use_fallback"})
    assert done.status == "done" and done.version.id == "v2", done
    edited, version = current_scene(root, "s1")
    assert version.id == "v2"
    assert edited.element("e1").canonical.font.family_guess == "Brand Sans"
    assert edited.element("e1").canonical.font.weight == 700


def test_fallback_choice_has_exact_korean_and_english_copy():
    src = Path("keepframe/web/static/js/i18n.js").read_text()
    assert '"review.editChoice.use_fallback": "대체 폰트로 진행"' in src.split("en: {", 1)[0]
    assert '"review.editChoice.use_fallback": "Use the fallback font"' in src.split("en: {", 1)[1]
