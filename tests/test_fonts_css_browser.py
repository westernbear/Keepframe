"""Styled text: the Pillow raster (numpy preview, report, AE texture) and the composer CSS draw the same pixels."""
import cv2
import numpy as np
import pytest

from keepframe.analyze.composite import composite_scene
from keepframe.compose.composer import compose
from keepframe.fonts.registry import FontRegistry
from keepframe.ir.colour import delta_e, srgb_to_lab
from keepframe.ir.schema import (Background, Canonical, Element, FontGuess, Keyframe, Scene, TextEffect, TextStyle,
                                 Track)

pytestmark = pytest.mark.browser
REG = FontRegistry()
X, Y = 24, 40


def _scene(text, font, style, box, color="#ffffff", bg="#000000"):
    el = Element(id="t", kind="text", role="text", visible=(0, 1),
                 canonical=Canonical(width=box[0], height=box[1], anchor=(0.0, 0.0), text=text, color=color,
                                     font=font, style=style, texture="assets/t.png"),
                 tracks={"x": Track(keys=[Keyframe(t=0, v=float(X))]), "y": Track(keys=[Keyframe(t=0, v=float(Y))])})
    return Scene(id="s", size=(640, 360), fps=30, frames=2, background=Background(kind="color", value=bg), elements=[el])


def _write_texture(scene, d):
    from keepframe.fonts.raster import render_styled
    el = scene.elements[0]
    c = el.canonical
    img = render_styled(c.text.split("\n"), c.font, c.color, c.style, registry=REG, scene_dir=d,
                        box=(round(c.width), round(c.height)))
    (d / "assets").mkdir(exist_ok=True)
    cv2.imwrite(str(d / c.texture), img[..., [2, 1, 0, 3]])


def _both(scene, d, margin=0):
    from keepframe.render.renderer import load_frame, render
    c = scene.elements[0].canonical
    a = composite_scene(scene, d, 0)
    r = render(compose(scene, d, d / "c.html"), scene, d / "r", frames=[0], probe=False)
    b = load_frame(r.frames_dir / "f_00000.png")
    x0, y0 = int(X - margin), int(Y - margin)
    x1, y1 = int(X + c.width + margin), int(Y + c.height + margin)
    return a[y0:y1, x0:x1], b[y0:y1, x0:x1]


def _mask_stats(a, b):
    ma, mb = a.mean(-1) > 0.5, b.mean(-1) > 0.5
    iou = (ma & mb).sum() / max(1, (ma | mb).sum())
    def bbox(m):
        ys, xs = np.nonzero(m)
        return np.array([xs.min(), ys.min(), xs.max(), ys.max()], float)
    return float(iou), float(np.abs(bbox(ma) - bbox(mb)).max())


def _parity(tmp_path, text, font, style, box, **kw):
    scene = _scene(text, font, style, box, **kw)
    _write_texture(scene, tmp_path)
    return _both(scene, tmp_path)


def _font(family="Inter", weight=400, size=48.0, **kw):
    return FontGuess(family_guess=family, weight=weight, size_px=size, source="bundled", **kw)


@pytest.mark.parametrize("family,size,box,dx,dy", [("Inter", 48.0, (420, 64), 2.0, 0.0),
                                                   ("Playfair Display", 37.5, (420, 61), 0.0, 1.0),
                                                   ("Roboto", 30.0, (300, 44), 3.0, -2.0),
                                                   ("Montserrat", 52.0, (520, 37), -1.0, 0.0)])   # tight R27 box: negative leading
def test_css_matches_raster_plain(tmp_path, family, size, box, dx, dy):
    a, b = _parity(tmp_path, "Hamburgefonts AVWa", _font(family, size=size), TextStyle(dx=dx, dy=dy), box)
    iou, d = _mask_stats(a, b)
    print(f"plain {family} {size}: IoU {iou:.3f} bbox {d:.2f}")
    assert iou >= 0.90 and d <= 1.5


def test_css_matches_raster_tracking(tmp_path):
    for tr in (0.12, -0.04):
        a, b = _parity(tmp_path, "Wavy Tofu AVATAR", _font(size=40.0), TextStyle(tracking_em=tr), (600, 56))
        iou, d = _mask_stats(a, b)
        print(f"tracking {tr}: IoU {iou:.3f} bbox {d:.2f}")
        assert iou >= 0.90 and d <= 1.5


def test_css_matches_raster_weight(tmp_path):
    for family, w in (("Inter", 300), ("Inter", 800), ("Poppins", 700), ("Pretendard", 650)):
        a, b = _parity(tmp_path, "Weight Hamburg", _font(family, w, 44.0), None, (480, 60))
        iou, d = _mask_stats(a, b)
        print(f"weight {family} {w}: IoU {iou:.3f} bbox {d:.2f}")
        assert iou >= 0.90 and d <= 1.5


def test_css_matches_raster_shear(tmp_path):
    for theta in (12.0, -8.0):
        a, b = _parity(tmp_path, "Slanted\nTwo lines", _font(size=40.0), TextStyle(shear_deg=theta, dx=12), (420, 110))
        iou, d = _mask_stats(a, b)
        print(f"shear {theta}: IoU {iou:.3f} bbox {d:.2f}")
        assert iou >= 0.90 and d <= 1.5


def test_css_matches_raster_gradient(tmp_path):
    for fill in ({"kind": "linear", "angle": 180, "stops": [{"offset": 0, "color": "#ffd000"}, {"offset": 1, "color": "#ff2060"}]},
                 {"kind": "linear", "angle": 100, "stops": [{"offset": 0.1, "color": "#20e0ff"}, {"offset": 0.6, "color": "#ffffff"},
                                                            {"offset": 0.9, "color": "#ff40c0"}]},
                 {"kind": "radial", "center": [0.3, 0.6], "radius": 0.7, "stops": [{"offset": 0, "color": "#ffffff"}, {"offset": 1, "color": "#3050ff"}]}):
        scene = _scene("GRADIENT", _font(weight=900, size=72.0), TextStyle(fill=fill, dx=4), (460, 90), bg="#101010")
        _write_texture(scene, tmp_path)
        a, b = _both(scene, tmp_path)
        tex = cv2.imread(str(tmp_path / "assets" / "t.png"), cv2.IMREAD_UNCHANGED)[..., 3]
        inside = cv2.erode((tex >= 250).astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
        de = delta_e(srgb_to_lab((a * 255).round().astype(np.uint8)), srgb_to_lab((b * 255).round().astype(np.uint8)))[inside]
        p95 = float(np.percentile(de, 95))
        print(f"gradient {fill['kind']} {fill.get('angle')}: p95 dE {p95:.2f} over {inside.sum()} px")
        assert inside.sum() > 2000 and p95 <= 4


def test_css_matches_raster_effects(tmp_path):
    styles = [TextStyle(effects=[TextEffect(kind="stroke", color="#ff3000", width=3)], dx=6, dy=0),
              TextStyle(effects=[TextEffect(kind="shadow", color="#000000", opacity=0.7, dx=4, dy=5, blur=6)], dx=4),
              TextStyle(effects=[TextEffect(kind="glow", color="#40c0ff", opacity=0.9, blur=10),
                                 TextEffect(kind="stroke", color="#102080", width=2)], dx=10, dy=0),
              TextStyle(fill={"kind": "linear", "angle": 180, "stops": [{"offset": 0, "color": "#ffffff"}, {"offset": 1, "color": "#ffb000"}]},
                        effects=[TextEffect(kind="stroke", color="#000000", width=2.5),
                                 TextEffect(kind="shadow", color="#000000", opacity=0.6, dx=3, dy=3, blur=4)],
                        fade={"angle": 90, "stops": [{"offset": 0, "alpha": 1}, {"offset": 1, "alpha": 0.25}]}, dx=8, shear_deg=6)]
    for i, style in enumerate(styles):
        a, b = _parity(tmp_path, "Effects 42", _font(weight=800, size=56.0), style, (420, 84), color="#f0f0f0", bg="#5a6a7a")
        l1 = float(np.abs(a - b).mean())
        print(f"effects {i}: box L1 {l1:.4f}")
        assert l1 <= 0.03


def test_hangul_fallback_runs_match(tmp_path):
    cases = [(_font("Inter", 500, 40.0, fallback="Pretendard", fallback_weight=700, fallback_scale=1.08), TextStyle()),
             (_font("Playfair Display", 400, 36.0), TextStyle(tracking_em=0.04))]   # automatic Noto Serif KR fallback
    for font, style in cases:
        a, b = _parity(tmp_path, "Hello 한글 텍스트 OK", font, style, (520, 60))
        iou, d = _mask_stats(a, b)
        print(f"hangul {font.family_guess}: IoU {iou:.3f} bbox {d:.2f}")
        assert iou >= 0.85


def test_edited_text_preview_matches_html(tmp_path):
    from keepframe.edit.apply import apply_edit
    from keepframe.edit.intent import Target
    style = TextStyle(fill={"kind": "linear", "angle": 180, "stops": [{"offset": 0, "color": "#fff3a0"}, {"offset": 1, "color": "#ff7a00"}]},
                      effects=[TextEffect(kind="stroke", color="#401000", width=2),
                               TextEffect(kind="shadow", color="#000000", opacity=0.5, dx=3, dy=4, blur=6)], dx=3)
    scene = _scene("SALE", _font("Montserrat", 800, 52.0), style, (220, 70), bg="#2a3b4c")
    _write_texture(scene, tmp_path)
    for value in ("MEGA SALE", "Hi"):
        edited = apply_edit(scene, tmp_path, [Target(element="t", property="text", value=value)], {}, None)
        c = edited.elements[0].canonical
        assert c.text == value and c.style == style and c.texture != scene.elements[0].canonical.texture
        a, b = _both(edited, tmp_path)
        l1 = float(np.abs(a - b).mean())
        print(f"edited {value!r} box {c.width}x{c.height}: L1 {l1:.4f}")
        assert l1 <= 0.03


def test_generic_and_unknown_families_match(tmp_path):
    for family in ("sans-serif", "serif", "Zzz Missing Sans"):
        a, b = _parity(tmp_path, "Generic Family 42", FontGuess(family_guess=family, weight=400, size_px=40.0),
                       TextStyle(), (420, 56))
        iou, d = _mask_stats(a, b)
        print(f"generic {family}: IoU {iou:.3f} bbox {d:.2f}")
        assert iou >= 0.90 and d <= 1.5


@pytest.mark.parametrize("effect,fade", [
    (TextEffect(kind="glow", color="#ffe060", opacity=1.0, blur=24), None),
    (TextEffect(kind="shadow", color="#000000", opacity=0.8, dx=8, dy=10, blur=6), None),
    (TextEffect(kind="shadow", color="#000000", opacity=0.8, dx=8, dy=10, blur=6),
     {"angle": 90, "stops": [{"offset": 0.2, "alpha": 1}, {"offset": 1, "alpha": 0.2}]})])
def test_css_matches_raster_effects_past_the_box(tmp_path, effect, fade):
    """R41: a glow or shadow reaching past the (tight, R27) text box is drawn there by both sides — the CSS effect
    canvas (its fade mask clamped past the box) and the padded raster the numpy compositor places by `texture_pad`."""
    from keepframe.fonts.raster import natural_box, render_styled, resolve_fonts
    from keepframe.render.renderer import load_frame, render
    font, style = _font(weight=800, size=56.0), TextStyle(effects=[effect], dx=4, fade=fade)
    box = natural_box(["Glow 42"], 56.0, resolve_fonts(font, "Glow 42", REG), dx=4)
    scene = _scene("Glow 42", font, style, box, color="#f0f0f0", bg="#5a6a7a")
    el = scene.elements[0]
    x, y = 80, 70
    el.tracks = {"x": Track(keys=[Keyframe(t=0, v=float(x))]), "y": Track(keys=[Keyframe(t=0, v=float(y))])}
    rgba, pad = render_styled(["Glow 42"], font, "#f0f0f0", style, registry=REG, box=box, padded=True)
    (tmp_path / "assets").mkdir(exist_ok=True)
    cv2.imwrite(str(tmp_path / el.canonical.texture), rgba[..., [2, 1, 0, 3]])
    el.canonical.texture_pad = float(pad)
    a = composite_scene(scene, tmp_path, 0)
    r = render(compose(scene, tmp_path, tmp_path / "c.html"), scene, tmp_path / "r", frames=[0], probe=False)
    b = load_frame(r.frames_dir / "f_00000.png")
    region = (slice(y - pad, y + box[1] + pad), slice(x - pad, x + box[0] + pad))
    a, b = a[region], b[region]
    inside = np.zeros(a.shape[:2], bool)
    inside[pad:pad + box[1], pad:pad + box[0]] = True
    bg = np.float32([0x5a, 0x6a, 0x7a]) / 255
    drawn = [int((np.abs(img - bg).max(-1)[~inside] > 0.03).sum()) for img in (a, b)]
    print(f"{effect.kind}: pixels past the box raster {drawn[0]} css {drawn[1]}")
    assert min(drawn) > 100   # both draw the effect past the box
    l1, l1_out = float(np.abs(a - b).mean()), float(np.abs(a - b)[~inside].mean())
    print(f"{effect.kind} past the box: pad {pad} L1 {l1:.4f} outside {l1_out:.4f}")
    assert l1 <= 0.03 and l1_out <= 0.03
