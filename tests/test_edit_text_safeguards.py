"""Task 12: text edits keep the analysed colour, style, effects and tracks; fonts resolve uploaded > bundled >
Hangul fallback > system > Hershey; effects past the text box survive (R41); absurd sizes are clamped (R40)."""
import hashlib
import math

import cv2
import numpy as np
import pytest

from keepframe.analyze.composite import composite_scene
from keepframe.edit.agent import edit
from keepframe.edit.apply import apply_edit
from keepframe.edit.intent import Intent, Target, plan
from keepframe.fonts.registry import FontRegistry
from keepframe.fonts.upload import store_font
from keepframe.ir.colour import delta_e, hex_to_rgb8, srgb_to_lab
from keepframe.ir.schema import (Background, Canonical, Element, FontGuess, Keyframe, Scene, TextEffect, TextStyle,
                                 Track)
from keepframe.ir.store import current_scene, init_project, scene_dir
from tests.test_font_upload import _fake_render, _ttf

REG = FontRegistry()
RED, BLUE = "#ff3020", "#0030ff"


def _tracks():
    k = lambda *pairs: Track(keys=[Keyframe(t=t, v=v) for t, v in pairs])
    return {"x": k((0, 40.0)), "y": k((0, 60.0)), "opacity": k((0, 0.0), (2, 1.0)), "reveal": k((0, 0.2), (2, 1.0)),
            "rx": k((0, 0.0), (3, 12.0)), "ry": k((0, -8.0)), "sx": k((0, 1.0))}


def _natural(text, font, style):
    from keepframe.fonts.raster import natural_box, resolve_fonts
    return natural_box([text], font.size_px, resolve_fonts(font, text, REG), tracking_em=style.tracking_em,
                       shear_deg=style.shear_deg, dx=style.dx)


def _text_scene(text, font, style, color=RED, box=None, size=(640, 200)):
    w, h = box or _natural(text, font, style or TextStyle())
    el = Element(id="t1", kind="text", role="text", visible=(0, 4), tracks=_tracks(), z=Track(keys=[Keyframe(t=0, v=3)]),
                 canonical=Canonical(width=float(w), height=float(h), anchor=(0.0, 0.0), text=text, color=color,
                                     font=font, style=style, texture="assets/t1.png"))
    return Scene(id="s1", size=size, fps=30, frames=5, background=Background(kind="color", value="#ffffff"), elements=[el])


def _mask(rgb, hexs, tol=40.0):
    return delta_e(srgb_to_lab(rgb.astype(np.float32)), srgb_to_lab(np.float32(hex_to_rgb8(hexs)))) < tol


def _bbox(m):
    ys, xs = np.nonzero(m)
    return np.array([xs.min(), ys.min(), xs.max(), ys.max()], float)


def test_title_edit_keeps_analysed_colour_effects_and_tracks(tmp_path):
    style = TextStyle(dx=2, effects=[TextEffect(kind="shadow", color=BLUE, opacity=1.0, dx=6, dy=16, blur=0)])
    font = FontGuess(family_guess="Montserrat", weight=800, size_px=52, source="bundled")
    scene = _text_scene("SALE", font, style)
    before = scene.model_copy(deep=True)
    out = apply_edit(scene, tmp_path, [Target(element="t1", property="text", value="MEGA SALE")], {"overflow": "expand_box"}, None)
    el, old = out.element("t1"), before.element("t1")
    c = el.canonical
    assert (c.text, c.color, c.style, c.font.family_guess, c.font.weight, c.font.size_px) == (
        "MEGA SALE", RED, style, "Montserrat", 800, 52)
    assert el.tracks == old.tracks and el.visible == old.visible and el.z == old.z and scene == before
    frame = composite_scene(out, tmp_path, 3) * 255
    red, blue = _mask(frame, RED), _mask(frame, BLUE)
    med = np.median(frame[red], axis=0)
    assert float(delta_e(srgb_to_lab(np.float32(med)), srgb_to_lab(np.float32(hex_to_rgb8(RED))))) < 5
    shift = _bbox(blue)[2:] - _bbox(red)[2:]              # right/bottom edges: the shadow sits (dx, dy) past the glyphs
    assert np.abs(shift - [6, 16]).max() <= 1.5, shift
    ys, xs = np.nonzero(blue)
    assert ((xs >= 40 + c.width) | (ys >= 60 + c.height)).sum() >= 20   # it reaches past the text box (R41)


def _uploaded_project(tmp_path):
    root = tmp_path / "proj"
    style = TextStyle(tracking_em=0.05, effects=[TextEffect(kind="stroke", color="#000000", width=2)])
    font = FontGuess(family_guess="Inter", weight=700, size_px=40, source="bundled")
    scene = _text_scene("Sale", font, style, color="#ffffff", box=(420, 60))
    init_project(root, {"file": "ref.mp4", "fps": 30, "size": list(scene.size)}, scene)
    c = scene.element("t1").canonical
    from keepframe.fonts.raster import render_styled
    rgba = render_styled([c.text], c.font, c.color, c.style, registry=REG, box=(c.width, c.height))
    (scene_dir(root, "s1") / "assets").mkdir(parents=True)
    cv2.imwrite(str(scene_dir(root, "s1") / c.texture), rgba[..., [2, 1, 0, 3]])
    up = tmp_path / "up.ttf"
    up.write_bytes(_ttf())
    store_font(root, up, "brand.ttf")
    return root, scene


def test_font_edit_to_uploaded_family(tmp_path, monkeypatch):
    from keepframe.fonts.raster import render_styled
    root, scene = _uploaded_project(tmp_path)
    _fake_render(monkeypatch)
    intent = Intent(targets=[Target(element="t1", property="font", value="Brand Wide")])
    assert not plan(scene, intent, fonts=FontRegistry.for_project(root)).conflicts   # the registry before fontconfig
    done = edit(root, "s1", "폰트를 Brand Wide로", confirm=True, intent=intent)
    assert done.status == "done", done
    edited, _ = current_scene(root, "s1")
    el, old = edited.element("t1"), scene.element("t1")
    c, sd = el.canonical, scene_dir(root, "s1")
    sha = hashlib.sha256(_ttf()).hexdigest()
    assert (c.font.family_guess, c.font.source, c.font.file) == ("Brand Wide", "uploaded", f"assets/font-{sha[:16]}.ttf")
    assert c.style == old.canonical.style and c.color == old.canonical.color and el.tracks == old.tracks
    tex = cv2.imread(str(sd / c.texture), cv2.IMREAD_UNCHANGED)[..., [2, 1, 0, 3]]
    want, pad = render_styled([c.text], c.font, c.color, c.style, registry=FontRegistry.for_project(root), scene_dir=sd,
                              box=(c.width, c.height), padded=True)
    assert c.texture_pad == pad and np.array_equal(tex, want)


@pytest.mark.parametrize("family,style", [("Inter", TextStyle()), ("DejaVu Sans", None)])
def test_hangul_text_on_latin_font_uses_fallback(tmp_path, family, style):
    """A Latin primary (bundled, or a system family on a legacy element) draws Hangul with the bundled fallback,
    ahead of any system Hangul font (fontconfig would pick WenQuanYi here)."""
    from keepframe.compose.composer import _element_html
    from keepframe.fonts.raster import render_styled, resolve_fonts
    font = FontGuess(family_guess=family, weight=400, size_px=32, source="bundled" if style else "system")
    scene = _text_scene("Sale", font, style, color="#ffffff", box=(300, 44))
    out = apply_edit(scene, tmp_path, [Target(element="t1", property="text", value="가을 세일 Sale")], {}, None)
    c = out.element("t1").canonical
    assert c.style == (style or TextStyle())                 # a legacy element now draws the styled way
    fonts = resolve_fonts(c.font, c.text, REG)
    assert fonts.primary.family == family and fonts.fallback is not None and fonts.fallback.source == "bundled"
    assert fonts.fallback.family in {"Pretendard", "Noto Sans KR"}
    tex = cv2.imread(str(tmp_path / c.texture), cv2.IMREAD_UNCHANGED)[..., [2, 1, 0, 3]]
    want, pad = render_styled([c.text], c.font, c.color, c.style, registry=REG, box=(c.width, c.height), padded=True)
    assert np.array_equal(tex, want) and c.texture_pad == pad
    assert "kf-text" in _element_html(out.element("t1"), tmp_path, 30, None, REG)


def test_synthetic_title_edit_has_no_smear(tmp_path, monkeypatch):
    """A picture plate (the ig2 case) and a title with a soft shadow past its box: the retexted title leaves no
    smear and no changed pixels outside the glyphs."""
    from keepframe.fonts.raster import render_styled
    from keepframe.ir.synth import ground_truth, make_reference_scene, render_frames, true_plate
    from keepframe.qa.edits import edit_checks
    from keepframe.verify.verifier import VerifyReport
    tdir = tmp_path / "truth"
    truth = make_reference_scene(tdir, seed=4, plate="image", n_sprites=1, logo=False, frames=20)
    title = truth.element("title1")
    c = title.canonical
    c.style = (c.style or TextStyle()).model_copy(update={"effects": [
        TextEffect(kind="shadow", color="#000000", opacity=0.6, dx=3, dy=4, blur=6)]})
    rgba, pad = render_styled([c.text], c.font, c.color, c.style, box=(c.width, c.height), padded=True)
    cv2.imwrite(str(tdir / c.texture), cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA))
    c.texture_pad = float(pad)
    root = tmp_path / "proj"
    sd = root / "scenes" / "s1"
    import shutil
    shutil.copytree(tdir / "assets", sd / "assets")
    init_project(root, {"file": "ref.mp4", "fps": 30, "size": [640, 360], "mode": "range", "range": [0, 19]},
                 truth.model_copy(deep=True, update={"id": "s1"}))
    (sd / "stages").mkdir()
    np.save(sd / "stages" / "frames.npy", np.stack([f.round().astype(np.uint8) for f in render_frames(truth, tdir, range(20))]))
    monkeypatch.setattr("keepframe.edit.agent.render", lambda *_a, **_k: None)
    monkeypatch.setattr("keepframe.edit.agent.verify", lambda *_a, **_k: VerifyReport(
        schema_ok=True, keep_pass_rate=1, temporal=1, layer_probe_complete=True, passed=True))
    gt = ground_truth(truth, tdir, [19])
    res = edit_checks(root, "s1", title="title1", hide=[], frames=[19], renderer="numpy",
                      truth={"scene": truth, "dir": tdir, "layers": gt, "plates": {19: true_plate(truth, tdir, 19)},
                             "title": "title1", "pairs": {e.id: e.id for e in truth.elements}})
    t = res["title"]
    assert t["status"] == "done" and t["visible_frames"] == 1, t
    assert t["outside_glyph_delta"] <= 1.0 and t["smear_score"] <= 3 and t["glyph_de"] < 5, t


@pytest.mark.parametrize("overflow", [None, "wrap", "shrink_font", "expand_box"])
@pytest.mark.parametrize("styled", [False, True])
def test_text_edit_clamps_absurd_sizes(tmp_path, monkeypatch, overflow, styled):
    """R40: a scene's font size (untrusted) never reaches a raster unclamped, on the legacy path in every overflow
    mode and on the styled path's plain fallback."""
    from keepframe.edit import apply
    if styled:   # the styled raster fails: the plain fallback still draws a bounded texture
        monkeypatch.setattr(apply, "render_styled", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")))
    font = FontGuess(family_guess="Inter" if styled else "DejaVu Sans", weight=400, size_px=1e7)
    scene = _text_scene("Sale", font, TextStyle() if styled else None, color="#ffffff", box=(300, 40))
    plan(scene, Intent(targets=[Target(element="t1", property="text", value="Mega sale")]))   # measuring is bounded too
    out = apply_edit(scene, tmp_path, [Target(element="t1", property="text", value="Mega sale")], {"overflow": overflow}, None)
    c = out.element("t1").canonical
    tex = cv2.imread(str(tmp_path / c.texture), cv2.IMREAD_UNCHANGED)
    assert tex is not None and tex.shape[0] <= 2 * 8 * 40 + 64 and tex.shape[1] <= 9 * 8 * 40 * 1.5, tex.shape
    assert math.isfinite(c.width) and c.width <= 16384 and c.height <= 16384


def test_plan_checks_the_registry_and_measures_styled_text(monkeypatch):
    """Task 8 minors: a bundled family is not 'missing' because fontconfig lacks it, and the overflow check
    measures the text as it is drawn (weight, tracking), not at 400 without tracking."""
    font = FontGuess(family_guess="Inter", weight=900, size_px=40, source="bundled")
    scene = _text_scene("Sale", font, TextStyle(), color="#ffffff", box=(420, 56))
    built = plan(scene, Intent(targets=[Target(element="t1", property="font", value="Pretendard")]), fonts=REG)
    assert not [c for c in built.conflicts if c.id == "font_missing"]
    tracked = _text_scene("Sale", font, TextStyle(tracking_em=0.35), color="#ffffff", box=(420, 56))
    text = "Autumn Sale Now"
    plain_w = _natural(text, font.model_copy(update={"weight": 400}), TextStyle())[0]
    tracked_w = _natural(text, font, TextStyle(tracking_em=0.35))[0]
    assert plain_w <= 420 * 1.15 < tracked_w
    built = plan(tracked, Intent(targets=[Target(element="t1", property="text", value=text)]), fonts=REG)
    assert [c.id for c in built.conflicts] == ["overflow"]


def _legacy_hangul(text="가을 세일", family="Apple SD Gothic Neo"):
    font = FontGuess(family_guess=family, weight=700, size_px=40, source="system")
    return _text_scene(text, font, None, color="#ffffff", box=(300, 50))


def test_colour_edit_keeps_a_legacy_hangul_title_legacy(tmp_path):
    """Review fix 3: a colour-only edit never changes a legacy title's font (its named family stays in the CSS,
    the inspector and AE); the styled path (bundled Hangul fallback) is only for text edits that bring in Hangul
    the named face lacks."""
    import re
    from keepframe.compose.composer import _element_html
    scene = _legacy_hangul()
    out = apply_edit(scene, tmp_path, [Target(element="t1", property="color", value="#ff0000")], {}, None)
    c = out.element("t1").canonical
    assert c.style is None and c.font == scene.element("t1").canonical.font and c.color == "#ff0000"
    html = _element_html(out.element("t1"), tmp_path, 30, None, REG)
    assert "kf-text" not in html and re.findall(r"font-family:[^;\"]+", html) == ["font-family:Apple SD Gothic Neo"]
    retext = apply_edit(scene, tmp_path, [Target(element="t1", property="text", value="겨울 세일")], {}, None)
    assert retext.element("t1").canonical.style is None              # Hangul was already there: no switch
    latin = _legacy_hangul("Sale", "DejaVu Sans")
    brought = apply_edit(latin, tmp_path, [Target(element="t1", property="text", value="가을 Sale")], {}, None)
    assert brought.element("t1").canonical.style == TextStyle()    # DejaVu Sans lacks Hangul: the bundled fallback
    unknown = apply_edit(_legacy_hangul("Sale"), tmp_path, [Target(element="t1", property="text", value="가을 Sale")], {}, None)
    assert unknown.element("t1").canonical.style is None            # a family not on this machine: left to the browser


def test_replacing_a_texture_or_model_resets_the_texture_pad(tmp_path):
    """R54: only `_write_styled` writes padded textures; any other new texture covers the box exactly."""
    import base64
    style = TextStyle(effects=[TextEffect(kind="shadow", color=BLUE, dx=6, dy=8, blur=4)])
    font = FontGuess(family_guess="Inter", weight=700, size_px=40, source="bundled")
    scene = _text_scene("Sale", font, style)
    padded = apply_edit(scene, tmp_path, [Target(element="t1", property="text", value="Mega")], {}, None)
    assert padded.element("t1").canonical.texture_pad > 0
    ok, png = cv2.imencode(".png", np.full((20, 40, 4), 255, np.uint8))
    swapped = apply_edit(padded, tmp_path, [Target(element="t1", property="texture", value="attachment")], {},
                         png.tobytes())
    assert swapped.element("t1").canonical.texture_pad == 0
    from keepframe.assets import validate_glb
    import keepframe.edit.apply as apply_mod
    glb = b"glTF" + bytes(16)
    orig = apply_mod.validate_glb
    apply_mod.validate_glb = lambda data: data
    try:
        modelled = apply_edit(padded, tmp_path, [Target(element="t1", property="model", value="attachment")], {}, glb)
    finally:
        apply_mod.validate_glb = orig
    assert modelled.element("t1").canonical.texture_pad == 0
