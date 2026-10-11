"""Task 5 (Stage B): per-glyph reveal (`Canonical.glyphs`), drawn alike by numpy and Chromium (D15)."""
import cv2
import numpy as np
import pytest
from pydantic import ValidationError

from keepframe.analyze.composite import composite_scene
from keepframe.ir.schema import Background, Canonical, Element, GlyphReveal, Keyframe, Scene, Track
from keepframe.ir.synth import make_texture
from keepframe.ir.tracks import element_bbox, glyph_alphas, glyph_edges

QUARTERS = GlyphReveal(x=[0.125, 0.375, 0.625, 0.875], delays=[0, 2, 4, 6], ramp=4)


def k(*pairs) -> Track:
    return Track(keys=[Keyframe(t=t, v=v) for t, v in pairs])


def _rect_scene(sd, pad=0):
    """A white 40×10 box at (10, 5) whose four glyph columns are 10 px wide; visible from frame 3."""
    make_texture(sd / "assets" / "box.png", "rect", 40 + 2 * pad, 10 + 2 * pad, (255, 255, 255))
    el = Element(id="box", kind="sprite", visible=(3, 20), tracks={"x": k((0, 10)), "y": k((0, 5))},
                 canonical=Canonical(width=40, height=10, anchor=(0, 0), texture="assets/box.png", texture_pad=pad,
                                     glyphs=QUARTERS))
    return Scene(id="glyphs", size=(64, 24), fps=30, frames=21, background=Background(value="#000000"), elements=[el])


def test_glyph_reveal_validation():
    assert "glyphs" not in Canonical(width=1, height=1).model_dump()
    c = Canonical(width=40, height=10, glyphs=QUARTERS)
    assert Canonical.model_validate_json(c.model_dump_json()).glyphs == QUARTERS
    assert GlyphReveal(x=[0.5], delays=[0]).ramp == 1.0
    for bad in [dict(x=[], delays=[]), dict(x=[0.5] * 513, delays=[0] * 513), dict(x=[0.2, 0.8], delays=[0]),
                dict(x=[0.8, 0.2], delays=[0, 1]), dict(x=[-0.1], delays=[0]), dict(x=[1.1], delays=[0]),
                dict(x=[0.5], delays=[-1]), dict(x=[0.5], delays=[float("inf")]), dict(x=[0.5], delays=[0], ramp=0.5),
                dict(x=[0.5], delays=[0], ramp=121)]:
        with pytest.raises(ValidationError):
            GlyphReveal(**bad)
    assert glyph_alphas(QUARTERS, 4).tolist() == [1, 0.5, 0, 0]
    assert glyph_edges(QUARTERS, 40) == [0, 10, 20, 30, 40]
    assert glyph_edges(QUARTERS, 40.2) == [0, 10, 20, 30, 41]


def test_composite_glyph_columns(tmp_path):
    scene = _rect_scene(tmp_path)
    plain = scene.model_copy(deep=True)
    plain.elements[0].canonical.glyphs = None
    cache = {}
    got = composite_scene(scene, tmp_path, 7, cache)[..., 0]           # 4 frames in: α = 1, 0.5, 0, 0
    assert np.allclose(got[5:15, 10:20], 1) and np.allclose(got[5:15, 20:30], 0.5) and not got[:, 30:].any()
    assert not got[:5].any() and not got[15:].any()
    for f in (13, 20):                                                 # every glyph is in: no mask at all
        assert np.array_equal(composite_scene(scene, tmp_path, f, cache), composite_scene(plain, tmp_path, f))
    padded = _rect_scene(tmp_path / "pad", pad=3)                      # the padding past the box is cut, as the reveal does
    assert not composite_scene(padded, tmp_path / "pad", 7)[..., 0][:, :10].any()
    assert composite_scene(padded, tmp_path / "pad", 13)[..., 0][2:18, 7:10].all()


def test_retime_scales_glyph_delays():
    from keepframe.edit.retime import retime_element
    el = Element(id="t", kind="text", visible=(10, 40), canonical=Canonical(
        width=40, height=10, glyphs=GlyphReveal(x=[0.25, 0.75], delays=[0, 6], ramp=1.5)))
    retime_element(el, 0.5, 0, 0)
    assert el.canonical.glyphs.delays == [0, 12] and el.canonical.glyphs.ramp == 3
    retime_element(el, 4, 0, 0)
    assert el.canonical.glyphs.delays == [0, 3] and el.canonical.glyphs.ramp == 1   # a ramp stays ≥ 1 frame


def test_text_edit_resamples_glyphs(tmp_path):
    from keepframe.edit.apply import apply_edit
    from keepframe.edit.intent import Target
    from keepframe.edit.textraster import render_lines
    from keepframe.ir.schema import FontGuess
    font = FontGuess(family_guess="DejaVu Sans", size_px=20)
    texture = render_lines(["ABCD"], font.size_px, (255, 255, 255), font.family_guess)
    cv2.imwrite(str(tmp_path / "text.png"), texture)
    el = Element(id="title", kind="text", visible=(0, 20), canonical=Canonical(
        width=texture.shape[1], height=texture.shape[0], anchor=(0, 0), texture="text.png", text="ABCD", font=font,
        color="#ffffff", glyphs=QUARTERS))
    scene = Scene(id="t", size=(400, 64), fps=30, frames=21, background=Background(), elements=[el])
    edit = lambda text: apply_edit(scene, tmp_path, [Target(element="title", property="text", value=text)],
                                   {"overflow": "expand_box"}, None).elements[0].canonical.glyphs
    assert edit("WXYZ") == QUARTERS                                    # same glyph count: the measured centres stay
    g = edit("AB CDEF GH")                                             # 8 glyphs: uniform centres, the stagger's shape
    assert g.x == pytest.approx([(i + 0.5) / 8 for i in range(8)])
    assert g.delays == pytest.approx([0, 0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6]) and g.ramp == 4


def _ae_layer(tmp_path, delays):
    from keepframe.ae.spec import comp_spec
    el = Element(id="t", kind="group", visible=(2, 30), canonical=Canonical(
        width=100, height=20, glyphs=GlyphReveal(x=[0.1, 0.3, 0.5, 0.7, 0.9], delays=delays, ramp=4)))
    scene = Scene(id="s", size=(320, 180), fps=30, frames=60, background=Background(), elements=[el])
    spec = comp_spec(scene, tmp_path, project="p", scene_id="s", version="v1")
    return next(item for item in spec["layers"] if item["id"] == "kf:t")


def test_ae_glyph_reveal_exports_wipe_with_warning(tmp_path):
    delays = [0, 2, 4, 6, 8]
    layer = _ae_layer(tmp_path, delays)
    wipe = layer["effects"]["reveal"]
    assert wipe["angle"] == 270 and wipe["feather"] == 0
    assert "per-glyph reveal of t exported as a wipe" in layer["warnings"]
    keys = wipe["completion"]
    assert keys[0][:2] == [2, 100] and keys[-1][:2] == [14, 0] and len(keys) < 13   # 13 frames sampled
    g = GlyphReveal(x=[0.1, 0.3, 0.5, 0.7, 0.9], delays=delays, ramp=4)
    w = np.diff(glyph_edges(g, 100))
    for t, value, *_ in keys:                                          # every key sits on the revealed share of the columns
        assert value == pytest.approx(100 * (1 - glyph_alphas(g, t - 2) @ w / w.sum()), abs=1e-3)
    assert _ae_layer(tmp_path, delays[::-1])["effects"]["reveal"]["angle"] == 90


def test_lottie_refuses_glyph_reveal(tmp_path):
    from keepframe.render.lottie import preflight_lottie
    from keepframe.render.plan import PlanConflict
    scene = _rect_scene(tmp_path)
    with pytest.raises(PlanConflict, match="per-glyph reveal"):
        preflight_lottie(scene)
    scene.elements[0].canonical.glyphs = None
    preflight_lottie(scene)


@pytest.mark.browser
def test_css_glyph_mask_matches_numpy(tmp_path):
    """The gate: over 6 frames across the stagger, per-frame mean |Δ| ≤ 0.6 levels and p99 inside the title ≤ 8.
    A tilted twin's mask follows its transform: it adds nothing to the tilt's own difference (frame 30, every glyph in)."""
    from keepframe.compose.composer import compose
    from keepframe.edit.textraster import render_lines
    from keepframe.render.renderer import load_frame, render
    texture = render_lines(["KEEPFRAME"], 48, (255, 255, 255), "DejaVu Sans")
    cv2.imwrite(str(tmp_path / "title.png"), texture)
    g = GlyphReveal(x=[(i + 0.5) / 9 for i in range(9)], delays=[i * 2.5 for i in range(9)], ramp=5)

    def title(eid, x, y, **tracks):
        return Element(id=eid, kind="sprite", visible=(4, 40), tracks={"x": k((0, x)), "y": k((0, y)), **tracks},
                       canonical=Canonical(width=texture.shape[1], height=texture.shape[0], anchor=(0, 0),
                                           texture="title.png", glyphs=g))
    scene = Scene(id="g", size=(480, 270), fps=30, frames=41, background=Background(value="#182430"), elements=[
        title("flat", 96, 40), title("tilted", 96, 170, ry=k((0, 25)), rx=k((0, -10)))])
    frames = [5, 9, 13, 17, 21, 25, 30]
    r = render(compose(scene, tmp_path, tmp_path / "g.html"), scene, tmp_path / "r", frames=frames)
    p99 = {}
    for i, f in enumerate(frames):
        delta = np.abs(np.round(composite_scene(scene, tmp_path, f) * 255) -
                       np.round(load_frame(r.frames_dir / f"f_{i:05d}.png") * 255))
        assert delta.mean() <= 0.6, (f, delta.mean())
        for el in scene.elements:
            x0, y0, x1, y1 = (int(v) for v in np.round(element_bbox(el, f, scene.size)))
            p99[el.id, f] = np.percentile(delta[y0:y1, x0:x1], 99)
    assert max(p99["flat", f] for f in frames) <= 8, p99
    assert max(p99["tilted", f] for f in frames) <= p99["tilted", 30], p99
