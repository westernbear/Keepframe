import hashlib
import json
import math
import re
import shutil

import cv2
import numpy as np
import pytest

from keepframe.compose.composer import compose
from keepframe.fonts.registry import FontFace, FontRegistry
from keepframe.ir.schema import (Background, Canonical, Element, FontGuess, Keyframe, Scene, TextEffect, TextStyle,
                                 Track)

REG = FontRegistry()


def _ink_cols(a: np.ndarray) -> np.ndarray:
    return np.nonzero(a.max(0) > 0.5)[0]


def test_tracking_adds_n_times_t():
    from keepframe.fonts.raster import glyph_alpha
    face, size, t_em, text = REG.face("Inter"), 40.0, 0.1, "Hamburg"
    t = size * t_em
    a0 = glyph_alpha([text], size, [face], weight=400)
    a1 = glyph_alpha([text], size, [face], weight=400, tracking_em=t_em)
    assert a0.shape[0] == a1.shape[0]
    # CSS adds the spacing after every character, the last one included
    assert abs((a1.shape[1] - a0.shape[1]) - len(text) * t) <= 1
    # ... so the last glyph moves right by (n - 1) t
    assert abs((_ink_cols(a1)[-1] - _ink_cols(a0)[-1]) - (len(text) - 1) * t) <= 1
    neg = glyph_alpha([text], size, [face], weight=400, tracking_em=-0.05)
    assert abs((a0.shape[1] - neg.shape[1]) - len(text) * 0.05 * size) <= 1


def test_shear_offsets_top_row():
    from keepframe.fonts.raster import glyph_alpha
    face, theta = REG.face("Inter"), 12.0
    a0 = glyph_alpha(["I"], 64, [face], weight=400, box=(120, 80), dx=40)
    a1 = glyph_alpha(["I"], 64, [face], weight=400, box=(120, 80), dx=40, shear_deg=theta)
    rows = np.nonzero(a0.max(1) > 0.5)[0]
    baseline = rows[-1] + 1                      # "I" sits on the baseline
    for r in (rows[0] + 1, (rows[0] + rows[-1]) // 2, rows[-1] - 1):
        cx = [float((a[r] * np.arange(a.shape[1])).sum() / a[r].sum()) for a in (a0, a1)]
        want = math.tan(math.radians(theta)) * (baseline - (r + 0.5))   # positive shear leans right above the baseline
        assert abs((cx[1] - cx[0]) - want) <= 0.5, (r, cx, want)


def test_weight_axis_changes_coverage():
    from keepframe.fonts.raster import glyph_alpha
    light = glyph_alpha(["Hamburg"], 48, [REG.face("Inter", 300)], weight=300)
    heavy = glyph_alpha(["Hamburg"], 48, [REG.face("Inter", 800)], weight=800)
    assert heavy.sum() / light.sum() > 1.4
    # weights outside the axis clamp instead of failing (Pretendard reads 45-930)
    assert glyph_alpha(["Ag"], 32, [REG.face("Pretendard")], weight=950).sum() > 0
    # static families keep their files: Poppins 700 is a different face from 400
    p4, p7 = REG.face("Poppins", 400), REG.face("Poppins", 700)
    assert p4.path != p7.path
    assert glyph_alpha(["Ag"], 32, [p7], weight=700).sum() > 1.2 * glyph_alpha(["Ag"], 32, [p4], weight=400).sum()


def _square(h=40, w=60):
    a = np.zeros((h, w), np.float32)
    a[12:28, 20:40] = 1.0
    return a


def test_effects_forward_model():
    from keepframe.fonts.raster import compose_text
    alpha, blue = _square(), np.array([0.0, 0.0, 1.0], np.float32)
    shadow = TextEffect(kind="shadow", color="#ff0000", opacity=0.5, dx=3, dy=2, blur=4)
    out = compose_text(alpha, blue, [shadow])
    assert out.shape == (40, 60, 4) and out.dtype == np.float32
    shifted = cv2.warpAffine(alpha, np.float32([[1, 0, 3], [0, 1, 2]]), (60, 40))
    want = 0.5 * cv2.GaussianBlur(shifted, (0, 0), 2.0, borderType=cv2.BORDER_CONSTANT)   # sigma = blur / 2
    outside = alpha == 0
    assert np.allclose(out[..., 3][outside], want[outside], atol=2e-3)
    assert np.allclose(out[outside & (want > 0.01)][:, :3], [1, 0, 0], atol=1e-3)
    assert np.allclose(out[alpha == 1], [0, 0, 1, 1], atol=1e-6)

    stroke = TextEffect(kind="stroke", color="#00ff00", width=2)
    ring = compose_text(alpha, blue, [stroke])
    assert np.allclose(ring[10, 30], [0, 1, 0, 1]) and np.allclose(ring[29, 30], [0, 1, 0, 1])   # w px outside the fill
    assert ring[9, 30, 3] == 0 and ring[30, 30, 3] == 0
    assert np.allclose(ring[20, 30], [0, 0, 1, 1])                                                 # fill on top
    disk = cv2.dilate(alpha, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
    assert np.allclose(ring[..., 3], disk)

    glow = TextEffect(kind="glow", color="#ffffff", opacity=0.8, blur=6)
    both = compose_text(alpha, blue, [glow, stroke])   # order is shadows -> stroke -> fill whatever the list order
    assert np.allclose(both[10, 30, :3], [0, 1, 0]) and np.allclose(both[20, 30], [0, 0, 1, 1])
    g = 0.8 * cv2.GaussianBlur(alpha, (0, 0), 3.0, borderType=cv2.BORDER_CONSTANT)
    assert abs(both[6, 30, 3] - g[6, 30]) < 2e-3 and np.allclose(both[6, 30, :3], [1, 1, 1], atol=1e-3)



def test_render_styled_gradient_fade_and_box():
    from keepframe.fonts.raster import render_styled
    style = TextStyle(fill={"kind": "linear", "angle": 180, "stops": [{"offset": 0, "color": "#ff0000"}, {"offset": 1, "color": "#0000ff"}]},
                      fade={"angle": 90, "stops": [{"offset": 0, "alpha": 1}, {"offset": 1, "alpha": 0.2}]})
    font = FontGuess(family_guess="Inter", weight=800, size_px=60, source="bundled")
    img = render_styled(["HHHH"], font, "#00ff00", style, registry=REG, box=(260, 80))
    assert img.shape == (80, 260, 4) and img.dtype == np.uint8
    solid = img[..., 3] >= 40
    rows = np.nonzero(solid.any(1))[0]
    top, bot = img[rows[0] + 1][solid[rows[0] + 1]], img[rows[-1] - 1][solid[rows[-1] - 1]]
    assert top[:, 0].mean() > top[:, 2].mean() and bot[:, 2].mean() > bot[:, 0].mean()   # red at the top, blue at the bottom
    cols = np.nonzero(img[..., 3].max(0) > 0)[0]
    assert img[:, cols[0]:cols[0] + 8, 3].max() > img[:, cols[-1] - 8:cols[-1], 3].max() + 80   # fades to the right
    nat = render_styled(["HHHH"], font, "#00ff00", None, registry=REG)
    assert nat.shape[1] < 260 and nat[..., 3].max() == 255 and np.all(nat[nat[..., 3] == 255, :3] == (0, 255, 0))


def _el(eid, text, font, style=None, w=300, h=60, color="#ffffff"):
    return Element(id=eid, kind="text", role="text", visible=(0, 4),
                   canonical=Canonical(width=w, height=h, anchor=(0.0, 0.0), text=text, color=color, font=font, style=style),
                   tracks={"x": Track(keys=[Keyframe(t=0, v=10.0)]), "y": Track(keys=[Keyframe(t=0, v=20.0)])})


def test_fonts_css_only_used_families_safe_names(tmp_path):
    from keepframe.fonts.css import font_face_css, text_css
    els = [_el("a", "Hello", FontGuess(family_guess="Inter", weight=700, size_px=40, source="bundled")),
           _el("b", "가나 Hi", FontGuess(family_guess="Playfair Display", weight=400, size_px=30, source="bundled"),
               TextStyle(tracking_em=0.05)),
           _el("c", "legacy", FontGuess(family_guess="sans-serif", size_px=20))]
    scene = Scene(id="s", size=(320, 180), fps=30, frames=5, background=Background(), elements=els)
    css = font_face_css(scene, tmp_path, REG, cache_dir=tmp_path / "cache")
    names = re.findall(r"font-family:([^;}]+)", css)
    assert names and all(re.fullmatch(r"kf-[a-z0-9-]+", n) for n in names)
    assert {n for n in names if "-fb" not in n} == {"kf-inter", "kf-playfair-display"}
    fb = [n for n in names if "-fb" in n]
    assert len(fb) == 1 and fb[0].startswith("kf-noto-serif-kr-fb")      # serif -> Noto Serif KR for the Hangul run
    assert css.count("@font-face") == 3 and css.count("data:font/woff2;base64,") == 3
    assert "Montserrat" not in css and "kf-pretendard" not in css and "'" not in css and '"' not in css
    assert len(css) < 120_000                                              # subset to the used codepoints + Basic Latin
    assert "font-weight:100 900" in css and "size-adjust" in css
    assert list((tmp_path / "cache").glob("*.woff2"))
    assert font_face_css(scene, tmp_path, REG, cache_dir=tmp_path / "cache") == css   # cached and deterministic

    decl = text_css(els[1].canonical.font, "#ffffff", els[1].canonical.style, (300, 60), text="가나 Hi")
    assert "font-family:kf-playfair-display,kf-noto-serif-kr-fb" in decl and "letter-spacing:0.05em" in decl
    assert "text-rendering:geometricPrecision" in decl and "line-height:60px" in decl

    html = compose(scene, tmp_path, tmp_path / "c.html").read_text()
    assert html.count("@font-face") == 3
    assert '<span style="font-family:sans-serif;font-weight:400;font-size:20.0px;line-height:60.0px;color:#ffffff">legacy</span>' in html
    evil = "x';}\n</style><script>alert(1)</script>"

    class Hostile(FontRegistry):   # a system face whose name is hostile (FontGuess.fallback is not validated)
        def face(self, family, weight=400, *, italic=False):
            return FontFace(evil, "system", REG.face("Pretendard").path) if family == evil else super().face(family, weight, italic=italic)

    hostile = _el("d", "가 x", FontGuess(family_guess="Inter", fallback=evil, size_px=20), TextStyle())
    html = compose(scene.model_copy(update={"elements": els + [hostile]}), tmp_path, tmp_path / "c.html",
                   fonts=Hostile()).read_text()
    body = html.split("<body>", 1)[1].split("<script>", 1)[0]          # the element markup, before the page scripts
    assert 'id="el-d"' in body and "font-family:kf-inter,&#x27;xstylescriptalert1script&#x27;" in body
    assert "alert(1)" not in body and "</style><script>alert" not in html and "\n</style>" not in body


def _legacy_scene():
    els = [
        Element(id="t1", kind="text", role="text", visible=(0, 9),
                canonical=Canonical(width=200, height=40, anchor=(0.0, 0.0), text="Hello <world> & 한글", color="#ff0000",
                                    font=FontGuess(family_guess="sans-serif", weight=700, size_px=32)),
                tracks={"x": Track(keys=[Keyframe(t=0, v=10.0)]), "y": Track(keys=[Keyframe(t=0, v=20.0)])}),
        Element(id="t2", kind="text", role="text", visible=(2, 9),
                canonical=Canonical(width=120.5, height=30.25, text="Second", font=FontGuess(family_guess="DejaVu Sans", size_px=24.5))),
        Element(id="s1", kind="sprite", visible=(0, 9), canonical=Canonical(width=10, height=10)),
    ]
    return Scene(id="legacy", size=(320, 180), fps=30, frames=10, background=Background(kind="color", value="#102030"), elements=els)


def test_legacy_text_without_style_html_unchanged(tmp_path):
    html = compose(_legacy_scene(), tmp_path, tmp_path / "c.html").read_text()
    # sha256 of this page at 111b03b, before styled text existed
    assert hashlib.sha256(html.encode()).hexdigest() == "c2147a57610318d323c9adf64115722808e848b1729d882bdea106a36903bd56"
    assert "@font-face" not in html


def test_plan_pins_uploaded_font(tmp_path):
    from keepframe.ir.store import init_project
    from keepframe.render.plan import create_render_plan
    root = tmp_path / "p1"
    sd = root / "scenes" / "s1"
    (sd / "assets").mkdir(parents=True)
    name = "assets/font-0123456789abcdef.woff2"
    shutil.copyfile(REG.face("Inter").path, sd / name)
    font = FontGuess(family_guess="Brand Sans", weight=400, size_px=30, source="uploaded", file=name)
    scene = Scene(id="s1", size=(320, 180), fps=30, frames=6, background=Background(),
                  elements=[_el("t", "Brand", font, TextStyle())])
    init_project(root, {"file": "source.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, 5]}, scene)
    (root / "source.mp4").write_bytes(b"source")
    (root / "meta.json").write_text(json.dumps({"id": "p1", "status": "approved", "version": "v1", "scene": "s1"}), encoding="utf-8")
    plan = create_render_plan(root, project_id="p1", scene_id="s1", version_id="v1", backend="native", mode="final")
    pinned = {a.project_path: a for a in plan.assets}
    assert "scenes/s1/" + name in pinned
    assert pinned["scenes/s1/" + name].media_kind == "font/woff2"
    assert pinned["scenes/s1/" + name].sha256 == hashlib.sha256((sd / name).read_bytes()).hexdigest()


def test_uploaded_scene_font_drives_raster_and_css(tmp_path):
    from keepframe.fonts.css import font_face_css
    from keepframe.fonts.raster import resolve_fonts
    name = "assets/font-0123456789abcdef.woff2"
    (tmp_path / "assets").mkdir()
    shutil.copyfile(REG.face("Lobster").path, tmp_path / name)
    font = FontGuess(family_guess="Brand Script", weight=400, size_px=30, source="uploaded", file=name)
    fonts = resolve_fonts(font, "Brand", REG, tmp_path)
    assert fonts.primary.source == "uploaded" and fonts.primary.path == (tmp_path / name).resolve()
    scene = Scene(id="s", size=(320, 180), fps=30, frames=5, background=Background(), elements=[_el("t", "Brand", font)])
    css = font_face_css(scene, tmp_path, REG, cache_dir=tmp_path / "cache")
    assert re.search(r"font-family:kf-brand-script-[0-9a-f]{8};", css)
    bad = font.model_copy(update={"file": "../../etc/passwd"})
    assert resolve_fonts(bad, "Brand", REG, tmp_path).primary.source != "uploaded"


def test_textraster_draws_bundled_families(tmp_path):
    from keepframe.edit.textraster import measure, render_lines
    inter, fallback = render_lines(["Hamburg"], 40, (255, 255, 255), "Inter"), render_lines(["Hamburg"], 40, (255, 255, 255), "sans-serif")
    assert inter is not None and fallback is not None
    assert measure("Hamburg", 40, "Inter") == (inter.shape[1], inter.shape[0])
    assert inter.shape != fallback.shape or not np.array_equal(inter, fallback)   # Inter, not fontconfig's DejaVu


# --- fix round 1 (R33-R35) ---------------------------------------------------------------------------------

def test_generic_and_unknown_families_resolve_to_bundled_faces(tmp_path):
    from keepframe.fonts.css import font_face_css, text_css
    from keepframe.fonts.raster import resolve_fonts
    want = {"sans-serif": "Inter", "Zzz Missing Sans": "Inter", "system-ui": "Inter", "serif": "Source Serif 4",
            "monospace": "JetBrains Mono"}
    for family, bundled in want.items():
        fonts = resolve_fonts(FontGuess(family_guess=family), "Hi", REG)
        assert (fonts.primary.family, fonts.primary.source) == (bundled, "bundled"), family
    assert resolve_fonts(FontGuess(family_guess="sans-serif"), "가 Hi", REG).fallback.family == "Pretendard"
    assert resolve_fonts(FontGuess(family_guess="serif"), "가 Hi", REG).fallback.family == "Noto Serif KR"
    assert text_css(FontGuess(family_guess="sans-serif"), "#fff", TextStyle(), (200, 40), text="Hi").count("font-family:kf-inter;") == 1
    scene = Scene(id="s", size=(320, 180), fps=30, frames=5, background=Background(),
                  elements=[_el("a", "Hi", FontGuess(family_guess="sans-serif"), TextStyle()),
                            _el("b", "Hi", FontGuess(family_guess="serif"), TextStyle())])
    css = font_face_css(scene, tmp_path, REG, cache_dir=tmp_path / "cache")
    assert re.findall(r"font-family:([^;]+);", css) == ["kf-inter", "kf-source-serif-4"]


def _crafted_ttf(tmp_path, loca_end: int):
    """Inter as a plain TTF whose loca claims the glyphs run to `loca_end`."""
    import struct
    from keepframe.fonts.sfnt import sfnt_bytes
    data = bytearray(sfnt_bytes(REG.face("Inter").path))
    num = struct.unpack(">H", data[4:6])[0]
    for i in range(num):
        tag, _, offset, length = struct.unpack(">4sLLL", data[12 + 16 * i:28 + 16 * i])
        if tag == b"loca":
            data[offset + length - 4:offset + length] = struct.pack(">L", loca_end)
    path = tmp_path / "crafted.ttf"
    path.write_bytes(bytes(data))
    return path


def test_sfnt_caps_rebuilt_glyf_from_crafted_loca(tmp_path):
    import time
    from keepframe.fonts import sfnt
    t = time.time()
    assert sfnt.sfnt_bytes(_crafted_ttf(tmp_path, 0xFFFFFF00)) is None      # ~4 GB claim: refused, nothing allocated
    assert time.time() - t < 5
    tables = {"head": bytes(50) + b"\x00\x01" + bytes(2), "maxp": bytes(4) + b"\x00\x01", "glyf": bytes(10),
              "loca": (0).to_bytes(4, "big") + (200 * 2**20).to_bytes(4, "big")}
    assert sfnt._glyf_read_length(tables, 10**6) is None                     # past 64 MiB / 16x the file
    tables["loca"] = (0).to_bytes(4, "big") + (40).to_bytes(4, "big")
    assert sfnt._glyf_read_length(tables, 10**6) == 40


def test_sfnt_errors_fall_back_to_fonttools(monkeypatch):
    from keepframe.fonts import sfnt
    odd = {"head": bytes(50) + b"\x00\x01" + bytes(2), "maxp": bytes(4) + b"\x00\x01", "glyf": bytes(10), "loca": bytes(7)}
    assert sfnt._loca_ok(odd) is False                                       # odd-length loca: no exception
    def boom(_tables):
        raise ValueError("bad loca")
    monkeypatch.setattr(sfnt, "_loca_ok", boom)
    assert sfnt.sfnt_bytes(REG.face("Inter").path) is None


def test_failed_subset_embeds_no_whole_font(tmp_path, monkeypatch):
    from keepframe.fonts import css as fcss
    def boom(*_a, **_k):
        raise RuntimeError("cannot subset")
    monkeypatch.setattr(fcss, "_subset", boom)
    name = "assets/font-0123456789abcdef.woff2"
    (tmp_path / "assets").mkdir()
    shutil.copyfile(REG.face("Lobster").path, tmp_path / name)
    font = FontGuess(family_guess="Brand Script", size_px=30, source="uploaded", file=name)
    scene = Scene(id="s", size=(320, 180), fps=30, frames=5, background=Background(), elements=[_el("t", "Brand", font)])
    assert fcss.font_face_css(scene, tmp_path, REG, cache_dir=tmp_path / "cache") == ""
    html = compose(scene, tmp_path, tmp_path / "c.html").read_text()
    assert "data:font" not in html and 'class="kf-text"' in html


def test_subset_cache_writes_are_atomic_across_threads(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from keepframe.fonts.css import subset_font
    face = REG.face("Playfair Display")
    with ThreadPoolExecutor(6) as pool:
        out = list(pool.map(lambda _: subset_font(face, [0x41, 0x42], cache_dir=tmp_path), range(6)))
    assert len(set(out)) == 1 and out[0][:4] == b"wOF2"
    assert [p.name for p in tmp_path.iterdir() if not p.name.endswith(".woff2")] == []
    assert len(list(tmp_path.glob("*.woff2"))) == 1


def test_untrusted_sizes_are_bounded():
    import time
    from keepframe.fonts import raster as R
    from keepframe.fonts.css import text_html
    t = time.perf_counter()
    w, h, pad = R.bounded_canvas((1e6, 1e6), 10**6)
    assert max(w, h) <= R.MAX_BOX_SIDE and pad <= R.MAX_PAD and (w + 2 * pad) * (h + 2 * pad) <= R.MAX_CANVAS_PX
    assert R.bounded_canvas((3840, 2160), 40) == (3840, 2160, 40)              # a full 4K frame is not cropped
    assert R.MAX_TEXT_PX == 4096 and R.text_size(1e300) == 4096 and R.text_size(3000, 2160) == 3000
    assert R.text_size(1e6, 60) == 480                                         # <= 8x the line box
    with pytest.raises(ValueError):
        R.text_size(float("nan"))
    raw = TextStyle(tracking_em=1e6, shear_deg=89.9, dx=1e9, dy=-1e9,
                    effects=[TextEffect(kind="stroke", color="#000", width=1e6),
                             TextEffect(kind="shadow", color="#000", dx=1e9, dy=1e9, blur=1e6),
                             TextEffect(kind="glow", color="#fff", blur=float("inf"), opacity=7)])
    st = R.bounded_style(raw, (200, 60))
    assert (st.tracking_em, st.shear_deg, st.dx, st.dy) == (2.0, 60.0, 200.0, -200.0)
    assert [(e.width, e.blur, e.dx, e.dy, e.opacity) for e in st.effects] == [
        (32.0, 0.0, 0.0, 0.0, 1.0), (0.0, 128.0, 200.0, 200.0, 1.0), (0.0, 0.0, 0.0, 0.0, 1.0)]
    assert R.effect_pad(st.effects) <= R.MAX_PAD
    assert sum(map(len, R._cap_chars(["W" * 100000, "x"]))) == R.MAX_CHARS
    font = FontGuess(family_guess="Inter", size_px=1e6)
    css = text_html(Canonical(width=200, height=60, text="Hi", font=font, style=raw), registry=REG)
    for decl in ("font-size:480px", "letter-spacing:2em", "skewX(-60deg)", "filter:blur(64px)", "left:200px",
                 "-webkit-text-stroke:64px"):
        assert decl in css, decl
    assert time.perf_counter() - t < 1.0
    img = R.render_styled(["Hi"], font, "#fff", raw, registry=REG, box=(200, 60))   # one small end-to-end call
    assert img.shape == (60, 200, 4)


@pytest.mark.parametrize("size", [1e5, 1e300])
@pytest.mark.parametrize("styled", [True, False])
def test_shrink_font_edit_finishes_for_huge_sizes(tmp_path, size, styled):
    import time
    from keepframe.edit.apply import apply_edit, measure_text
    from keepframe.edit.intent import Target
    from keepframe.fonts.raster import natural_box, resolve_fonts
    font = FontGuess(family_guess="Inter" if styled else "sans-serif", size_px=size)
    scene = Scene(id="s", size=(320, 180), fps=30, frames=5, background=Background(),
                  elements=[_el("t", "Sale", font, TextStyle() if styled else None, w=200, h=60)])
    t = time.perf_counter()
    out = apply_edit(scene, tmp_path, [Target(element="t", property="text", value="Big sale today")],
                     {"overflow": "shrink_font"}, None)
    assert time.perf_counter() - t < 5
    c = out.elements[0].canonical
    assert 8 < c.font.size_px <= 4096 and c.width == 200
    if styled:
        assert natural_box(["Big sale today"], c.font.size_px, resolve_fonts(c.font, "Big sale today", REG))[0] <= 200
        assert natural_box(["Big sale today"], c.font.size_px + 2, resolve_fonts(c.font, "Big sale today", REG))[0] > 200
    else:
        assert measure_text("Big sale today", c.font.size_px)[0] <= 200 < measure_text("Big sale today", c.font.size_px + 2)[0]


# --- R36: subsetting never holds compose hostage -------------------------------------------------------------

@pytest.fixture
def fake_subset(monkeypatch):
    """`_subset` stub: per-family delay or failure, counting calls."""
    import time
    from keepframe.fonts import css as fcss
    calls, plan = [], {}
    def stub(face, cps, wght):
        calls.append(face.family)
        delay, fail = plan.get(face.family, (0.0, False))
        time.sleep(delay)
        if fail:
            raise RuntimeError("cannot subset")
        return b"wOF2" + face.family.encode() + bytes(1000)
    monkeypatch.setattr(fcss, "_subset", stub)
    return calls, plan


def _text_scene(*families):
    return Scene(id="s", size=(320, 180), fps=30, frames=5, background=Background(),
                 elements=[_el(f"e{i}", "Hi", FontGuess(family_guess=f), TextStyle()) for i, f in enumerate(families)])


def test_subset_keys_run_concurrently(tmp_path, fake_subset):
    import threading
    import time
    from keepframe.fonts.css import subset_font
    calls, plan = fake_subset
    plan["Lobster"] = (2.0, False)
    slow = threading.Thread(target=subset_font, args=(REG.face("Lobster"), [0x41]), kwargs={"cache_dir": tmp_path})
    slow.start()
    time.sleep(0.2)
    t = time.perf_counter()
    assert subset_font(REG.face("Inter"), [0x41], cache_dir=tmp_path).startswith(b"wOF2Inter")
    assert time.perf_counter() - t < 1.0              # another key does not wait for the slow one
    slow.join()
    assert sorted(calls) == ["Inter", "Lobster"]


def test_slow_subset_does_not_block_compose(tmp_path, fake_subset, monkeypatch):
    import time
    from keepframe.fonts import css as fcss
    calls, plan = fake_subset
    plan["Lobster"] = (1.5, False)
    monkeypatch.setattr(fcss, "SUBSET_WAIT_S", 0.3)
    scene = _text_scene("Inter", "Lobster")
    t = time.perf_counter()
    css = fcss.font_face_css(scene, tmp_path, REG, cache_dir=tmp_path / "c")
    assert time.perf_counter() - t < 1.0
    assert "kf-inter" in css and "kf-lobster" not in css            # that face falls back in the browser this time
    time.sleep(1.6)                                                  # the subset finished in the background ...
    assert "kf-lobster" in fcss.font_face_css(scene, tmp_path, REG, cache_dir=tmp_path / "c")   # ... and is cached
    assert calls.count("Lobster") == 1


def test_failed_subset_not_retried_on_every_compose(tmp_path, fake_subset):
    from keepframe.fonts import css as fcss
    calls, plan = fake_subset
    plan["Lobster"] = (0.0, True)
    scene = _text_scene("Lobster")
    assert fcss.font_face_css(scene, tmp_path, REG, cache_dir=tmp_path / "c") == ""
    assert fcss.font_face_css(scene, tmp_path, REG, cache_dir=tmp_path / "c") == ""
    assert calls == ["Lobster"]


def test_subset_cache_evicts_oldest(tmp_path, monkeypatch):
    import os
    import time
    from keepframe.fonts import css as fcss
    monkeypatch.setattr(fcss, "MAX_CACHE_BYTES", 120_000)
    face = REG.face("Playfair Display")
    paths = []
    for i in range(6):
        fcss.subset_font(face, [0x100 + i], cache_dir=tmp_path)
        newest = max(tmp_path.glob("*.woff2"), key=os.path.getmtime)
        paths.append(newest)
        assert sum(p.stat().st_size for p in tmp_path.glob("*.woff2")) <= 120_000
        time.sleep(0.01)
    assert paths[-1].exists() and not paths[0].exists()


def test_subset_caps_source_size_and_codepoints(tmp_path, monkeypatch):
    import io as _io
    from fontTools.ttLib import TTFont
    from keepframe.fonts import css as fcss
    monkeypatch.setattr(fcss, "MAX_CODEPOINTS", 120)
    data = fcss.subset_font(REG.face("Inter"), range(0x20, 0x3000), cache_dir=tmp_path)
    assert len(TTFont(_io.BytesIO(data)).getBestCmap()) <= 120
    monkeypatch.setattr(fcss, "MAX_SOURCE_BYTES", 1000)
    with pytest.raises(fcss.SubsetUnavailable):
        fcss.subset_font(REG.face("Lobster"), [0x41], cache_dir=tmp_path)
    assert fcss.font_face_css(_text_scene("Lobster"), tmp_path, REG, cache_dir=tmp_path) == ""


LAYOUT_AFTER_SUBSET = r"""
import sys, tempfile
from keepframe.fonts.css import subset_font
from keepframe.fonts.registry import FontRegistry
reg = FontRegistry()
subset_font(reg.face("Pretendard"), [0xac00], cache_dir=tempfile.mkdtemp())    # WOFF2 decoded by FreeType
from PIL import ImageFont                                                      # Pillow's first text layout after it
width = ImageFont.truetype(str(reg.face("Inter").path), 100).getlength("Big")
libs = open("/proc/self/maps").read() if sys.platform == "linux" else ""
hb = sorted({l.split()[-1] for l in libs.splitlines() if "libharfbuzz" in l and "pillow.libs" not in l})
print(width, hb)
"""


def test_subsetting_leaves_pillow_text_layout_intact(tmp_path):
    """FreeType 2.13+ dlopens the system HarfBuzz when it opens a WOFF2 face; done in this process, Pillow's lazily
    bound text layout then measured garbage (advances of 1e5+ px). The decode runs in a child process."""
    import os
    import subprocess
    import sys
    env = dict(os.environ, KEEPFRAME_FONT_CACHE=str(tmp_path))
    r = subprocess.run([sys.executable, "-c", LAYOUT_AFTER_SUBSET], capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 0, r.stderr[-2000:]
    width, hb = r.stdout.strip().splitlines()[-1].split(" ", 1)
    assert 100 < float(width) < 250 and hb == "[]", r.stdout


def test_sfnt_child_runs_isolated_without_server_secrets(monkeypatch, tmp_path):
    """Final review: the FreeType decode child, like the upload check child, runs with -P (no cwd on sys.path) and
    only the environment the interpreter needs (no API keys or tokens)."""
    import subprocess
    import sys
    from keepframe.fonts import sfnt
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret")
    monkeypatch.setenv("KEEPFRAME_ASSET_API_TOKEN", "tok-secret")
    calls = []
    real = subprocess.run

    def run(cmd, **kw):
        calls.append((cmd, kw.get("env")))
        return real(cmd, **kw)

    monkeypatch.setattr(sfnt.subprocess, "run", run)
    assert sfnt.sfnt_bytes(REG.face("Inter").path)                           # still decodes
    (cmd, env), = calls
    assert cmd[:3] == [sys.executable, "-P", "-B"] and cmd[3] == "-c"
    assert env is not None and not {"OPENAI_API_KEY", "KEEPFRAME_ASSET_API_TOKEN"} & env.keys()
    assert set(env) <= {"PATH", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "PYTHONNOUSERSITE", "LC_ALL", "SYSTEMROOT"}
