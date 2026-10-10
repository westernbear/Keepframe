"""Text style analysis (Task 9): fill colour, gradient, fade, effects and glyph measures from the matted texture
and the local plate."""
import json, pickle, time
from pathlib import Path
from types import SimpleNamespace
import cv2, numpy as np, pytest
from keepframe.analyze import matting, textstyle
from keepframe.analyze.background import foreground_mask_plate
from keepframe.analyze.pipeline import AnalyzeOptions, analyze, rerun
from keepframe.analyze.plate import PlateModel
from keepframe.analyze.text import TextBox, TextTrack, text_props
from keepframe.fonts.raster import compose_text, effect_pad, fade_alpha, glyph_alpha
from keepframe.fonts.registry import FontRegistry
from keepframe.ir.colour import delta_e, hex_to_rgb8, srgb_to_lab
from keepframe.ir.gradient import render_gradient
from keepframe.ir.schema import (AlphaStop, Background, Canonical, Element, Fade, Gradient, GradientStop, Keyframe, Scene,
                                 TextEffect, TextStyle, TextureMeta, Track)
from keepframe.ir.store import current_scene, new_version, scene_dir
from keepframe.ir.synth import render_frames

REG = FontRegistry()
W, H = 640, 360
CX, CY = 320, 180


def _de(a: str, b: str) -> float:
    return float(delta_e(srgb_to_lab(np.float32(hex_to_rgb8(a))), srgb_to_lab(np.float32(hex_to_rgb8(b)))))


def _grad(*stops: str, angle: float = 90.0) -> Gradient:
    return Gradient(kind="linear", angle=angle,
                    stops=[GradientStop(offset=i / (len(stops) - 1), color=c) for i, c in enumerate(stops)])


def _plate(bg: Background) -> PlateModel:
    if bg.kind == "gradient":
        img = render_gradient(bg.gradient, W, H)
        return PlateModel("gradient", img, tuple(int(v) for v in img.reshape(-1, 3).mean(0)), None, gradient=bg.gradient)
    rgb = hex_to_rgb8(bg.value)
    return PlateModel("color", np.full((H, W, 3), rgb, np.uint8), rgb, None)


def _even(a: np.ndarray) -> np.ndarray:
    h, w = a.shape[:2]
    return np.pad(a, ((0, h % 2), (0, w % 2)) + ((0, 0),) * (a.ndim - 2))


def _render(tmp_path, items, bg, n, noise, seed):
    """items: (id, rgba uint8, x, y, z). Frames rendered by the numpy compositor plus Gaussian noise."""
    (tmp_path / "assets").mkdir(parents=True, exist_ok=True)
    els = []
    for eid, rgba, x, y, z in items:
        cv2.imwrite(str(tmp_path / "assets" / f"{eid}.png"), cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA))
        els.append(Element(id=eid, kind="sprite", canonical=Canonical(width=rgba.shape[1], height=rgba.shape[0], texture=f"assets/{eid}.png"),
                           visible=(0, n - 1), z=Track(keys=[Keyframe(t=0, v=z)]),
                           tracks={"x": Track(keys=[Keyframe(t=0, v=float(x))]), "y": Track(keys=[Keyframe(t=0, v=float(y))])}))
    scene = Scene(id="s", size=(W, H), fps=30, frames=n, background=bg, elements=els)
    out = np.stack(render_frames(scene, tmp_path, range(n)))
    out += np.random.default_rng(seed).normal(0, noise, out.shape).astype(np.float32)
    return np.clip(np.rint(out), 0, 255).astype(np.uint8)


def _box_raw(ink: np.ndarray, tex_shape, n: int):
    """The detector's box around the ink of a texture centred at (CX, CY), and its raw rows (a held element)."""
    th, tw = tex_shape
    ys, xs = np.nonzero(ink)
    bx0, by0, bx1, by1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
    x0, y0 = CX - tw // 2 + bx0, CY - th // 2 + by0
    row = [x0 + (bx1 - bx0) / 2, y0 + (by1 - by0) / 2, 1, 1, 0, 0, 0, 1, 1]
    return (bx0, by0, bx1, by1), (x0, y0, x0 + bx1 - bx0, y0 + by1 - by0), np.tile(np.array(row, np.float64), (n, 1))


def _binary(frames, plate, box, f=0):
    x0, y0, x1, y1 = box
    return foreground_mask_plate(frames[f][y0:y1, x0:x1], plate.at(f)[y0:y1, x0:x1])


def _case(tmp_path, text="Sale", family="Inter", weight=700, size=48, fill="#a8001c", effects=(), fade=None,
          bg=None, n=8, noise=1.0, seed=0, grain=0.0, others=(), ghost=None):
    """A styled title (glyph_alpha + compose_text, the renderer's own model) held at the frame centre, matted the
    way the pipeline mattes text (detector box, padding 0), then analysed."""
    bg = bg or Background(kind="gradient", gradient=_grad("#f6e7c8", "#6fa8bf"))
    effects = list(effects)
    face = REG.face(family, weight)
    epad = effect_pad(effects) + 4
    a = _even(glyph_alpha([text], size, [face], weight=weight, pad=epad))
    th, tw = a.shape
    fill_map = (fill(a) if callable(fill)   # a map built from the glyphs (H×W×3, 0..1)
                else render_gradient(fill, tw, th).astype(np.float32) / 255 if isinstance(fill, Gradient)
                else np.float32(hex_to_rgb8(fill)) / 255)
    if grain:
        fill_map = np.clip(np.broadcast_to(fill_map, (th, tw, 3))
                           + np.random.default_rng(seed + 7).normal(0, grain / 255, (th, tw, 3)), 0, 1).astype(np.float32)
    rgba = compose_text(a, fill_map, effects)
    width = max([e.width for e in effects if e.kind == "stroke"], default=0)
    ink = (cv2.dilate((a > 0.05).astype(np.uint8), np.ones((2 * int(np.ceil(width)) + 1,) * 2, np.uint8)) > 0) if width else a > 0.05
    tbox, box, raw = _box_raw(ink, (th, tw), n)
    if fade is not None:   # over the text's box, as the renderer draws it
        bx0, by0, bx1, by1 = tbox
        rgba[by0:by1, bx0:bx1, 3] *= fade_alpha(fade, bx1 - bx0, by1 - by0)
    tex = np.clip(np.rint(rgba * 255), 0, 255).astype(np.uint8)
    extra = []
    if ghost is not None:   # an unmodelled copy of the title: (dx, dy, opacity), drawn under it, no layer for it
        gdx, gdy, gop = ghost
        g = tex.copy()
        g[..., 3] = np.rint(g[..., 3] * gop).astype(np.uint8)
        extra = [("ghost", g, CX + gdx, CY + gdy, 4)]
    frames = _render(tmp_path, [*others, *extra, ("t", tex, CX, CY, 5)], bg, n, noise, seed)
    plate = _plate(bg)
    others_at = None
    if others:
        layers = [matting.Layer(matting._premultiplied(o), matting.element_affine([x, y, 1, 1, 0, 0, 0, 1, 1], o.shape[:2], o.shape[1::-1]),
                                1.0, z > 5, o[..., 3].copy()) for _, o, x, y, z in others]
        others_at = lambda f: layers
    binary = _binary(frames, plate, box)
    rgba_m, meta = matting.texture_v2(raw, binary, frames, plate, others_at=others_at, pad=0, kind_hint="text", ref_frame=0)
    bx0, by0, bx1, by1 = tbox
    return SimpleNamespace(frames=frames, plate=plate, raw=raw, rgba=rgba_m, meta=meta, box=box, text=text, others_at=others_at,
                           alpha=a[by0:by1, bx0:bx1], fill=fill_map, tex_shape=(th, tw), tbox=tbox, binary=binary)


def _analyse(c, core=None):
    return textstyle.analyse_text_style(c.rgba, c.meta, c.frames, c.plate, c.raw, c.text, others_at=c.others_at,
                                        core=core)


def _core(c, bg_hex: str, box=None) -> str:
    """Today's core-mask colour of the case's text (what style_props passes as `core`): the most contrasting decile
    of the detector's box against the global background."""
    track = TextTrack(1, {f: TextBox(f, c.text, box or c.box, 0.99) for f in range(len(c.frames))}, c.text)
    return text_props(track, c.frames, hex_to_rgb8(bg_hex), len(c.frames), 0, infer_font=True, plate=None)[4]


def _kinds(style):
    return sorted(e.kind for e in style.effects)


# --- fill ---------------------------------------------------------------------------------------------------------

def _disc(d, rgb):
    s = 4
    m = np.zeros((d * s, d * s), np.uint8)
    cv2.circle(m, (d * s // 2, d * s // 2), d * s // 2 - s, 255, -1, cv2.LINE_AA)
    a = cv2.resize(m.astype(np.float32) / 255, (d, d), interpolation=cv2.INTER_AREA)
    out = np.zeros((d, d, 4), np.uint8)
    out[..., :3], out[..., 3] = rgb, np.rint(a * 255)
    return out


def test_fill_colour_uses_local_plate(tmp_path):
    """ig2: a dark red title over a pale globe on a dark plate. Today's colour is the most contrasting decile against
    the global background (the pale globe); the analysed fill is measured against the local plate (globe below).
    That wrong core colour, passed in, does not override it: the texture has no solid pixels of it."""
    bg = Background(kind="color", value="#2a0a14")
    globe = ("globe", _disc(150, hex_to_rgb8("#ffd9e2")), CX, CY, 1)
    c = _case(tmp_path, text="Big Sale", size=44, fill="#a8001c", bg=bg, others=[globe])
    old = _core(c, "#2a0a14")
    assert _de(old, "#a8001c") > 20
    style, colour, info = _analyse(c, core=old)
    assert _de(colour, "#a8001c") < 5
    assert style.fill is None and style.fade is None and style.effects == []
    assert info["spread"] < 6


def test_fill_ignores_a_baked_plate(tmp_path):
    """A texture that kept the plate (α 1 over its whole box), against the right plate and against one ~20 ΔE off
    (an animated background kept as a still): the fill is still the glyphs' colour, and nothing else is claimed."""
    c = _case(tmp_path, text="Limited edition", weight=400, size=36, fill="#ab0004",
              bg=Background(kind="gradient", gradient=_grad("#e6a8c8", "#d494b8")))
    x0, y0, x1, y1 = c.box
    rgba = np.dstack([c.frames[0][y0:y1, x0:x1], np.full((y1 - y0, x1 - x0), 255, np.uint8)])
    off = _plate(Background(kind="gradient", gradient=_grad("#f8d0e4", "#f0bcd8")))
    de = delta_e(srgb_to_lab(off.image[y0:y1, x0:x1].astype(np.float32)), srgb_to_lab(c.plate.image[y0:y1, x0:x1].astype(np.float32)))
    assert float(np.median(de)) > 15
    for plate in (c.plate, off):
        style, colour, _ = textstyle.analyse_text_style(rgba, c.meta, c.frames, plate, c.raw, c.text)
        assert _de(colour, "#ab0004") < 5
        assert style.fill is None and style.fade is None and style.effects == []


def test_fill_ignores_a_baked_blob(tmp_path):
    """A matted texture with a thick unmodelled blob in it (a mover baked in): the dominant colour of the thin glyph
    interior is still the fill."""
    c = _case(tmp_path, text="New Arrivals", size=44, fill="#ab0004",
              bg=Background(kind="gradient", gradient=_grad("#fdf6e3", "#d8c9a7")))
    rgba = c.rgba.copy()
    rgba[:, :70, :3] = (6, 214, 160)
    rgba[:, :70:6, :3] = (255, 255, 255)
    rgba[:, :70, 3] = 255
    style, colour, _ = textstyle.analyse_text_style(rgba, c.meta, c.frames, c.plate, c.raw, c.text)
    assert _de(colour, "#ab0004") < 5 and style.fill is None


def test_fill_is_the_dominant_colour_not_the_median(tmp_path):
    """Thin stripes of three other colours over 60 % of the glyph interior (a striped mover crossing the title, baked
    in): no one colour is a majority, so a per-channel median mixes them; the dominant colour is still the fill."""
    c = _case(tmp_path, text="New Arrivals", size=44, fill="#ab0004",
              bg=Background(kind="gradient", gradient=_grad("#101a3a", "#2c4a6e")))
    rgba = c.rgba.copy()
    for k, rgb in ((0, (6, 214, 160)), (1, (255, 255, 255)), (2, (255, 209, 102))):
        rgba[:, k::5, :3] = rgb
    style, colour, _ = textstyle.analyse_text_style(rgba, c.meta, c.frames, c.plate, c.raw, c.text)
    assert _de(colour, "#ab0004") < 5


def test_texture_pixels_that_are_plate_are_not_fill(tmp_path):
    """A texture that kept the plate between its glyphs but not over its whole box (a clear 2 px margin): pixels
    within ΔE 12 of the local plate are plate, so the fill is the glyphs' colour, not the plate's."""
    c = _case(tmp_path, text="Limited edition", weight=400, size=36, fill="#ab0004",
              bg=Background(kind="gradient", gradient=_grad("#e6a8c8", "#d494b8")))
    x0, y0, x1, y1 = c.box
    alpha = np.zeros((y1 - y0, x1 - x0), np.uint8)
    alpha[2:-2, 2:-2] = 255
    rgba = np.dstack([c.frames[0][y0:y1, x0:x1], alpha])
    assert (alpha == 255).mean() < textstyle.BOX_SHARE
    style, colour, _ = textstyle.analyse_text_style(rgba, c.meta, c.frames, c.plate, c.raw, c.text)
    assert _de(colour, "#ab0004") < 5 and style.fill is None


def test_no_effect_in_the_fill_colour(tmp_path):
    """An unmodelled copy of the title a few px off at half opacity (a duplicate text track, a misregistered frame):
    the residual is the title's own colour — not a shadow (seed 4's false shadow was this)."""
    c = _case(tmp_path, text="Arrivals", size=48, weight=700, fill="#ab0004",
              bg=Background(kind="gradient", gradient=_grad("#fdf6e3", "#d8c9a7")), ghost=(-3, -2, 0.5))
    style, colour, _ = _analyse(c)
    assert style.effects == [] and _de(colour, "#ab0004") < 5


def test_no_false_effects_over_a_wrong_plate(tmp_path):
    """The plate behind the title is off by a smooth field (an animated background kept as a still): no glow or
    shadow explains that."""
    c = _case(tmp_path, text="Limited edition", size=40, fill="#ab0004",
              bg=Background(kind="gradient", gradient=_grad("#e6a8c8", "#b05a8c")))
    off = Background(kind="gradient", gradient=_grad("#f0b8d4", "#a04c80", angle=120.0))
    style, colour, _ = textstyle.analyse_text_style(c.rgba, c.meta, c.frames, _plate(off), c.raw, c.text)
    assert style.effects == [] and _de(colour, "#ab0004") < 5


def test_fill_of_a_texture_that_kept_its_surroundings(tmp_path):
    """envato1's shape: light glyphs whose matte kept the dark box around them (opaque over ~88 % of it, a few
    transparent holes), over a plate estimate ΔE ≈ 26 off that box (a haze baked into it). Every texture pixel is
    then ≥ 12 from the plate, so the plate keeps the whole box and its dark mode was the fill, with a white "glow":
    the fill is the glyphs' colour, and nothing else is claimed."""
    box_rgb = "#09011a"
    c = _case(tmp_path, text="Build Promo", size=48, fill="#f0ebfe", bg=Background(kind="color", value=box_rgb))
    x0, y0, x1, y1 = c.box
    box = (x0 - 8, y0 - 8, x1 + 8, y1 + 8)   # the detector's box and a margin (same centre: the raw rows hold)
    ink = cv2.dilate(_binary(c.frames, c.plate, box).astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    h, w = ink.shape
    hole = np.zeros((h, w), bool)
    hole[:, int(0.55 * w):int(0.7 * w)] = True
    hole[int(0.8 * h):, :int(0.3 * w)] = True
    alpha = np.where(hole & ~ink, 0, 255).astype(np.uint8)
    rgba = np.dstack([c.frames[0][box[1]:box[3], box[0]:box[2]], alpha])
    border = np.concatenate([alpha[0], alpha[-1], alpha[:, 0], alpha[:, -1]])
    assert 0.8 < (alpha == 255).mean() < textstyle.BOX_SHARE and (border == 255).mean() > 0.5
    haze = _plate(Background(kind="color", value="#13043b"))
    assert 20 < _de("#13043b", box_rgb) < textstyle.KEPT_PLATE_DE
    core = _core(c, box_rgb)
    style, colour, _ = textstyle.analyse_text_style(rgba, c.meta, c.frames, haze, c.raw, c.text, core=core)
    assert _de(colour, "#f0ebfe") < 5
    assert style.effects == [] and style.fade is None and style.confidence <= 0.5


def test_giant_glyph_that_touches_its_box(tmp_path):
    """envato1 t1's shape: one huge letter whose box is its own bounds, the matte keeping the dark surroundings in
    the corners and counters, over a hazy plate estimate. The glyph makes most of the opaque border, so a border in
    the fill's colour is no sign the fill is the surroundings: the fill stays the letter's colour."""
    box_rgb = "#09011a"
    c = _case(tmp_path, text="B", size=230, weight=600, fill="#f3f0f8", bg=Background(kind="color", value=box_rgb))
    x0, y0, x1, y1 = c.box
    x0, y0, x1, y1 = x0 + 6, y0 + 6, x1 - 6, y1 - 6   # the box cuts the letter (same centre: the raw rows hold)
    rgba = np.dstack([c.frames[0][y0:y1, x0:x1], np.full((y1 - y0, x1 - x0), 255, np.uint8)])
    border = np.concatenate([rgba[0], rgba[-1], rgba[:, 0], rgba[:, -1]])[:, :3].astype(np.float32)
    assert float((delta_e(srgb_to_lab(border), srgb_to_lab(np.float32(hex_to_rgb8("#f3f0f8")))) < 20).mean()) > 0.5
    haze = _plate(Background(kind="color", value="#13043b"))
    style, colour, _ = textstyle.analyse_text_style(rgba, c.meta, c.frames, haze, c.raw, c.text, core=_core(c, box_rgb))
    assert _de(colour, "#f3f0f8") < 10 and style.effects == []


def test_a_shape_kept_behind_the_title_is_not_the_fill(tmp_path):
    """Synthetic seed 6's shape: a teal title partly over a dark-teal block the analysis missed; the matte kept the
    block and the core-mask colour is the block's (it stands out more from the beige plate). The core's colour has
    plenty of solid pixels, but it stands out from the plate no more than the fill does, so it is no glyph inside
    kept background: the fill stays the title's."""
    block = np.zeros((60, 90, 4), np.uint8)
    block[...] = (*hex_to_rgb8("#073b4c"), 255)
    bg = Background(kind="color", value="#e8d5b7")
    c = _case(tmp_path, text="Up to 50% off", size=32, weight=700, fill="#118ab2", bg=bg,
              others=[("block", block, CX + 60, CY, 1)])
    x0, y0, x1, y1 = c.box
    crop = c.frames[0][y0:y1, x0:x1]
    rgba = np.dstack([crop, np.where(foreground_mask_plate(crop, c.plate.at(0)[y0:y1, x0:x1]), 255, 0).astype(np.uint8)])
    core = _core(c, "#e8d5b7")
    assert _de(core, "#073b4c") < 5
    style, colour, _ = textstyle.analyse_text_style(rgba, c.meta, c.frames, c.plate, c.raw, c.text, core=core)
    assert _de(colour, "#118ab2") < 5


def _ramp_and_blue(a: np.ndarray, share: float = 0.6) -> np.ndarray:
    """A fill map: the words left of the gap column nearest `share` of the glyph area a dark ramp down the glyph
    rows (#1d1d1e → #8a8a8c), the rest a flat light blue (ig3: "and it builts" / "for you")."""
    th, tw = a.shape
    cols = a.sum(0)
    cs = np.cumsum(cols) / cols.sum()
    gaps = np.flatnonzero(cols < 1e-3)
    split = int(gaps[np.argmin(np.abs(cs[gaps] - share))])
    ys = np.flatnonzero(a.max(1) > 0.05)
    t = np.clip((np.arange(th, dtype=np.float32) + 0.5 - ys.min()) / (ys.max() + 1 - ys.min()), 0, 1)[:, None, None]
    out = np.broadcast_to(np.float32(hex_to_rgb8("#c4dbf8")) / 255, (th, tw, 3)).copy()
    out[:, :split] = ((1 - t) * np.float32(hex_to_rgb8("#1d1d1e")) + t * np.float32(hex_to_rgb8("#8a8a8c"))) / 255
    return out


def test_two_colour_line_takes_the_larger_glyph_area(tmp_path):
    """ig3's shape: one line in two colours, ~60 % of the glyph area a dark vertical ramp, the rest a flat light blue.
    The blue is the density peak (the ramp spreads), so it was the fill of the whole line; the fill is the colour
    covering more glyph area, and the style says one fill cannot draw the line (lower confidence, `unfilled`)."""
    c = _case(tmp_path, text="and it builts for you", size=40, weight=500, fill=_ramp_and_blue,
              bg=Background(kind="gradient", gradient=_grad("#fdfdfd", "#f2f2f4")))
    bx0, by0, bx1, by1 = c.tbox
    fill = c.fill[by0:by1, bx0:bx1]
    dark = c.alpha * (fill[..., 2] < 0.5)
    assert 0.55 < dark.sum() / c.alpha.sum() < 0.65
    style, colour, info = _analyse(c)
    ramp = srgb_to_lab(np.float32(np.linspace(hex_to_rgb8("#1d1d1e"), hex_to_rgb8("#8a8a8c"), 32)))
    assert float(delta_e(ramp, srgb_to_lab(np.float32(hex_to_rgb8(colour)))).min()) < 6
    assert _de(colour, "#c4dbf8") > 40
    assert info["unfilled"] > textstyle.UNFILLED and style.confidence < c.meta.confidence


# --- real textures (the first Stage A clip run, R66: envato1, ig3, ig2) ---------------------------------------------

DATA = Path(__file__).parent / "data" / "text_fill"
REAL = json.loads((DATA / "cases.json").read_text())["cases"]


def _real(case: str):
    """The texture and the plate under it (scaled back to the texture's size; None: unknown in the real run)."""
    rgba = cv2.cvtColor(cv2.imread(str(DATA / f"{case}_rgba.png"), cv2.IMREAD_UNCHANGED), cv2.COLOR_BGRA2RGBA)
    h, w = rgba.shape[:2]
    if not REAL[case]["plate"]:
        return rgba, None
    plate = cv2.resize(cv2.cvtColor(cv2.imread(str(DATA / f"{case}_plate.png")), cv2.COLOR_BGR2RGB), (w, h),
                       interpolation=cv2.INTER_LINEAR)
    return rgba, plate


def _analyse_real(case: str):
    """analyse_text_style on a real texture: one frame rebuilt as the texture over its plate (edge-padded by the
    effect crop's margin), the texture held at its centre. An unknown plate: no measured transform, no crop."""
    rgba, plate = _real(case)
    a = rgba[..., 3].astype(np.float32) / 255
    pe = int(max(4, round(0.5 * textstyle.cap_height(a))))
    B = np.pad(plate if plate is not None else np.zeros_like(rgba[..., :3]), ((pe, pe), (pe, pe), (0, 0)), mode="edge")
    ap = np.pad(a, pe)[..., None]
    frame = np.rint(np.pad(rgba[..., :3], ((pe, pe), (pe, pe), (0, 0))) * ap + (1 - ap) * B).astype(np.uint8)
    th, tw = frame.shape[:2]
    raw = np.array([[tw / 2, th / 2, 1, 1, 0, 0, 0, 1, 1] if plate is not None else [np.nan] * 9], np.float64)
    meta = TextureMeta(method=REAL[case]["method"], frames=[0], confidence=REAL[case]["confidence"])
    return textstyle.analyse_text_style(rgba, meta, frame[None], B, raw, REAL[case]["text"], core=REAL[case]["core"])


@pytest.mark.parametrize("case", sorted(REAL))
def test_real_fills_are_the_glyph_colour(case):
    """envato1: the matte kept the near-black around the white glyphs (fill was that black, plus a white glow); ig3:
    a dark line with a light-blue word (fill was the blue); ig2: a red title over a pink globe (right, must stay);
    ig1 t24 / ig2 t21: dark handwriting on a white card / sticker the matte kept (fill was the white)."""
    style, colour, info = _analyse_real(case)
    want = REAL[case]
    assert _de(colour, want["want"]) < want["within"], (colour, want)
    if case == "envato1":
        assert style.effects == [] and style.confidence <= 0.5
    if case == "ig3":
        assert _de(colour, want["fill_9aff0a8"]) > 40
        assert info["unfilled"] > textstyle.UNFILLED and style.confidence < want["confidence"]


def test_core_fill_on_real_textures():
    """The core-mask colour breaks the tie only with the texture's own evidence: envato1's old fill is its kept
    near-black, far from the core, and the core's colour covers much of the solid pixels, so their colour is the
    fill; a fill that agrees with the core, or a core the texture has too few solid pixels of, changes nothing."""
    def lab(hexv):
        return srgb_to_lab(np.float32(hex_to_rgb8(hexv)))
    rgba, plate = _real("envato1")
    F, a, core = rgba[..., :3], rgba[..., 3], lab(REAL["envato1"]["core"])
    got = textstyle.core_fill(F, a, lab(REAL["envato1"]["fill_9aff0a8"]), core, plate.reshape(-1, 3))
    assert got is not None and float(delta_e(got, core)) < 10   # the near-black is the hazy plate's, the core is not
    assert textstyle.core_fill(F, a, lab(REAL["envato1"]["want"]), core) is None
    for case in ("ig2", "ig3"):
        rgba, _ = _real(case)
        F, a = rgba[..., :3], rgba[..., 3]
        assert textstyle.core_fill(F, a, lab(REAL[case]["want"]), lab(REAL[case]["core"])) is None
    rgba, _ = _real("ig3")   # its core is dark: a light-green core has no solid pixels to stand on
    assert textstyle.core_fill(rgba[..., :3], rgba[..., 3], lab(REAL["ig3"]["want"]), lab("#7fff7f")) is None


@pytest.mark.parametrize("case", ["ig3", "ig2"])
def test_fill_colour_by_glyph_area_on_real_textures(case):
    """ig3: the light-blue word is the density peak, the dark ramp covers more of the strokes; ig2: the pink globe
    rims the interior keeps are nearly half of it, but they are no stroke, so the red stays."""
    rgba, plate = _real(case)
    hexv, _, _ = textstyle.fill_colour(rgba, textstyle.stroke_width(rgba[..., 3]), plate)
    assert _de(hexv, REAL[case]["want"]) < REAL[case]["within"], hexv


def _gradient_p90(c, g: Gradient, truth: Gradient) -> float:
    """p90 ΔE76 between the analysed and the true fill over the solid glyph interior (box coordinates)."""
    bx0, by0, bx1, by1 = c.tbox
    want = render_gradient(truth, c.tex_shape[1], c.tex_shape[0])[by0:by1, bx0:bx1]
    got = render_gradient(g, bx1 - bx0, by1 - by0)
    inner = cv2.erode((c.alpha > 0.5).astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    de = delta_e(srgb_to_lab(got[inner].astype(np.float32)), srgb_to_lab(want[inner].astype(np.float32)))
    return float(np.percentile(de, 90))


def test_two_stop_gradient_detected(tmp_path):
    truth = _grad("#ff3d00", "#ffd600", angle=180.0)
    c = _case(tmp_path, text="HOT DEAL", size=56, weight=800, fill=truth,
              bg=Background(kind="gradient", gradient=_grad("#101a3a", "#2c4a6e")))
    style, colour, _ = _analyse(c)
    g = style.fill
    assert g is not None and g.kind == "linear" and len(g.stops) == 2
    assert min(g.angle % 180, 180 - g.angle % 180) < 15   # vertical, either direction (stops follow)
    assert _gradient_p90(c, g, truth) < 6


@pytest.mark.parametrize("a,b,angle", [("#ffffff", "#ff3d00", 90.0), ("#ff0080", "#00c0ff", 90.0), ("#ff0080", "#00c0ff", 180.0)])
def test_strong_gradients_get_their_stops(tmp_path, a, b, angle):
    """Stops far apart (ΔE 50–110): the ramp is followed across the whole fill, not extrapolated from the band around
    the dominant colour, and fitted in sRGB, where the renderer interpolates."""
    truth = _grad(a, b, angle=angle)
    c = _case(tmp_path, text="SUMMER SALE", size=56, weight=800, fill=truth,
              bg=Background(kind="gradient", gradient=_grad("#101a3a", "#2c4a6e")))
    style, _, _ = _analyse(c)
    assert style.fill is not None and style.fade is None
    assert _gradient_p90(c, style.fill, truth) < 6


def test_flat_noisy_text_has_no_gradient(tmp_path):
    c = _case(tmp_path, text="Grainy", size=52, fill="#2a6f97", grain=6.0, noise=2.5,
              bg=Background(kind="gradient", gradient=_grad("#fff4e0", "#f2c6a0")))
    style, colour, info = _analyse(c)
    assert style.fill is None
    assert _de(colour, "#2a6f97") < 4


def test_fade_detected(tmp_path):
    truth = Fade(angle=180.0, stops=[AlphaStop(offset=0.0, alpha=1.0), AlphaStop(offset=1.0, alpha=0.2)])
    c = _case(tmp_path, text="Fading", size=60, weight=800, fill="#ffffff", fade=truth,
              bg=Background(kind="gradient", gradient=_grad("#0b132b", "#3a506b")))
    style, colour, _ = _analyse(c)
    assert style.fade is not None
    bx0, by0, bx1, by1 = c.tbox
    want = fade_alpha(truth, bx1 - bx0, by1 - by0)
    got = fade_alpha(style.fade, bx1 - bx0, by1 - by0)
    inner = c.alpha > 0.9
    assert float(np.abs(got - want)[inner].mean()) < 0.08
    assert _de(colour, "#ffffff") < 5


# --- effects ------------------------------------------------------------------------------------------------------

def test_shadow_recovered(tmp_path):
    shadow = TextEffect(kind="shadow", color="#1a0a3a", opacity=0.7, dx=4, dy=5, blur=4)
    c = _case(tmp_path, text="Shadow", size=52, weight=800, fill="#ffffff", effects=[shadow])
    style, colour, _ = _analyse(c)
    assert _kinds(style) == ["shadow"]
    e = style.effects[0]
    assert abs(e.dx - 4) <= 1 and abs(e.dy - 5) <= 1 and abs(e.blur - 4) <= 2
    assert _de(e.color, "#1a0a3a") < 8
    assert _de(colour, "#ffffff") < 5


def test_glow_recovered(tmp_path):
    glow = TextEffect(kind="glow", color="#ffd27a", opacity=0.9, blur=16)
    c = _case(tmp_path, text="Glow", size=56, weight=800, fill="#1b1f5e", effects=[glow],
              bg=Background(kind="gradient", gradient=_grad("#0d0d1a", "#2b1d3f")))
    style, colour, _ = _analyse(c)
    assert _kinds(style) == ["glow"]
    e = style.effects[0]
    assert e.dx == 0 and e.dy == 0 and 8 <= e.blur <= 32
    assert _de(e.color, "#ffd27a") < 10
    assert _de(colour, "#1b1f5e") < 5


@pytest.mark.parametrize("width", [2.0, 3.0, 5.0])
def test_stroke_width_within_1px(tmp_path, width):
    stroke = TextEffect(kind="stroke", color="#111111", width=width)
    c = _case(tmp_path, text="Stroke", size=60, weight=800, fill="#ffcc00", effects=[stroke])
    style, colour, info = _analyse(c)
    assert _kinds(style) == ["stroke"]
    e = style.effects[0]
    assert abs(e.width - width) <= 1 and _de(e.color, "#111111") < 8
    assert _de(colour, "#ffcc00") < 5
    # the fill's own stem (the stroke ring is not part of it) and the measure on its own
    assert abs(info["stroke_px"] - textstyle.stroke_width(c.alpha)) <= 1


@pytest.mark.parametrize("bar", [2.0, 3.0, 4.5, 7.0, 10.0])
def test_stroke_width_of_bars_within_1px(bar):
    s = 8
    hi = np.zeros((60 * s, 120 * s), np.float32)
    for x in (20, 60, 100):
        hi[10 * s:50 * s, int((x - bar / 2) * s):int((x + bar / 2) * s)] = 1
    a = cv2.resize(hi, (120, 60), interpolation=cv2.INTER_AREA)
    assert abs(textstyle.stroke_width(a) - bar) <= 0.5
    assert abs(textstyle.cap_height(a) - 40) <= 1


TITLES = [("Summer Sale", "Inter", 700, 48), ("Weekend", "Roboto", 400, 40), ("Grand Opening", "Playfair Display", 700, 36),
          ("NEW DROP", "Montserrat", 800, 44), ("Fresh Picks", "Lobster", 400, 46), ("Limited", "Oswald", 500, 50),
          ("Hello World", "Poppins", 600, 34), ("Night Market", "Bebas Neue", 400, 56), ("Daily News", "Merriweather", 400, 30),
          ("Big Deals", "Inter", 900, 52), ("Coffee Time", "Pacifico", 400, 38), ("Studio", "Lexend", 300, 54),
          ("Open Late", "Source Serif 4", 600, 42), ("Mega Sale", "Anton", 400, 50), ("Spring", "Dancing Script", 700, 56),
          ("Launch Day", "Work Sans", 500, 40), ("Tech Talk", "JetBrains Mono", 400, 32), ("Holiday", "Nunito", 800, 48),
          ("Final Call", "DM Sans", 700, 44), ("Welcome", "Pretendard", 600, 46)]
PLATES = [_grad("#fdf6e3", "#93a1a1"), _grad("#0b132b", "#1c2541", angle=180.0), _grad("#ffe1ea", "#ff6f91"),
          _grad("#e0f7fa", "#80deea", angle=45.0), _grad("#222222", "#555555")]
FILLS = ["#a8001c", "#ffffff", "#1b1f5e", "#ffcc00", "#000000"]


def test_no_false_effects_on_20_clean_titles(tmp_path):
    seen = []
    for i, (text, family, weight, size) in enumerate(TITLES):
        if REG.face(family, weight) is None:
            pytest.fail(f"{family} is not bundled")
        plate = PLATES[i % len(PLATES)]
        fill = next(f for f in FILLS[i % len(FILLS):] + FILLS
                    if _de(f, plate.stops[0].color) > 40 and _de(f, plate.stops[-1].color) > 40)
        c = _case(tmp_path / f"t{i}", text=text, family=family, weight=weight, size=size, fill=fill,
                  bg=Background(kind="gradient", gradient=plate), seed=i)
        style, colour, _ = _analyse(c)
        seen.append((text, _kinds(style), style.fill is not None, style.fade is not None, round(_de(colour, fill), 2)))
    bad = [s for s in seen if s[1] or s[2] or s[3] or s[4] >= 5]
    assert not bad, bad


def test_stroke_ratio_tracks_weight(tmp_path):
    ratios = []
    for w in (300, 400, 700, 900):
        c = _case(tmp_path / str(w), text="Hamburgefonts", family="Inter", weight=w, size=48, fill="#1b1f5e",
                  bg=Background(kind="gradient", gradient=_grad("#fdf6e3", "#d8c9a7")))
        style, _, _ = _analyse(c)
        clean = textstyle.stroke_width(c.alpha) / textstyle.cap_height(c.alpha)
        assert style.stroke_ratio == pytest.approx(clean, rel=0.10), w
        ratios.append(style.stroke_ratio)
    assert ratios == sorted(ratios) and ratios[-1] > 1.8 * ratios[0]


def test_glyph_centres_count_matches():
    face = REG.face("Inter", 700)
    for text in ("Hamburg", "Big Sale 2026", "illicit", "AVATAR"):
        a = glyph_alpha([text], 40, [face], weight=700, pad=2)
        centres = textstyle.glyph_centres(a, text)
        assert centres is not None and len(centres) == len(text.replace(" ", "")), text
        assert centres == sorted(centres)
    two = glyph_alpha(["Big", "Sale"], 40, [face], weight=700, pad=2)
    assert len(textstyle.glyph_centres(two, "Big\nSale")) == 7
    assert textstyle.glyph_centres(np.zeros((20, 40), np.float32), "Hi") is None


def test_style_speed(tmp_path):
    c = _case(tmp_path, text="Summer Collection 2026", size=40, fill="#a8001c",
              effects=[TextEffect(kind="shadow", color="#000000", opacity=0.6, dx=3, dy=3, blur=4)])
    _analyse(c)
    t0 = time.perf_counter()
    for _ in range(3):
        _analyse(c)
    assert (time.perf_counter() - t0) / 3 < 0.5


# --- pipeline -----------------------------------------------------------------------------------------------------

@pytest.fixture
def styled_clip(tmp_path, monkeypatch):
    """A dark red Inter title with a dark shadow over a gradient, through `analyze` with a fake detector."""
    face = REG.face("Inter", 700)
    shadow = TextEffect(kind="shadow", color="#000000", opacity=0.6, dx=3, dy=3, blur=2)
    a = _even(glyph_alpha(["Sale"], 44, [face], weight=700, pad=effect_pad([shadow]) + 4))
    tex = np.clip(np.rint(compose_text(a, np.float32(hex_to_rgb8("#a8001c")) / 255, [shadow]) * 255), 0, 255).astype(np.uint8)
    bg = Background(kind="gradient", gradient=_grad("#fff4e0", "#9ad1d4"))
    frames = _render(tmp_path / "src", [("t", tex, CX, CY, 1)], bg, 8, 1.0, 0)
    _, box, _ = _box_raw(a > 0.05, a.shape, 8)

    class FakeOcr:
        def __init__(self, max_side=None):
            pass

        def __call__(self, frame):
            return [("Sale", box, 0.95)]

    monkeypatch.setattr("keepframe.analyze.pipeline.read_frames", lambda *args: (frames, 30.0))
    monkeypatch.setattr("keepframe.analyze.text.RapidOcr", FakeOcr)
    root = tmp_path / "project"
    analyze(tmp_path / "clip.mp4", 0, 7, root, AnalyzeOptions(refine=False, use_ecc=False, generate_3d=False), ocr=FakeOcr())
    return root


def _title(root):
    scene, _ = current_scene(root, "s1")
    return scene, next(e for e in scene.elements if e.kind == "text")


def test_pipeline_stores_style_and_colour(styled_clip):
    scene, el = _title(styled_clip)
    assert _de(el.canonical.color, "#a8001c") < 5
    style = el.canonical.style
    assert style is not None and style.stroke_ratio and 0.05 < style.stroke_ratio < 0.4
    assert _kinds(style) == ["shadow"]
    report = json.loads((scene_dir(styled_clip, "s1") / "report.json").read_text())
    assert not [m for m in report["messages"] if "text style" in m]


def test_style_failure_keeps_core_colour_with_message(styled_clip, monkeypatch):
    sd = scene_dir(styled_clip, "s1")
    props = pickle.loads((sd / "stages/props.pkl").read_bytes())
    key = next(k for k, p in props.items() if isinstance(p, dict) and p.get("kind") == "text")
    monkeypatch.setattr(textstyle, "analyse_text_style", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    rerun(styled_clip, "s1", "sprites", note="style fails")
    _, el = _title(styled_clip)
    props = pickle.loads((sd / "stages/props.pkl").read_bytes())
    assert el.canonical.style is None and el.canonical.color == props[key]["core_color"]
    report = json.loads((sd / "report.json").read_text())
    assert f"{el.id}: text style failed; kept the core-mask colour" in report["messages"]   # R51: no exception text
    assert not [m for m in report["messages"] if "boom" in m]


def test_rerun_keeps_manual_text_style(styled_clip):
    scene, el = _title(styled_clip)
    el.provenance = "manual"
    el.canonical.text = "Mega"
    el.canonical.style = TextStyle(effects=[TextEffect(kind="stroke", color="#00ff00", width=2)], tracking_em=0.05)
    new_version(styled_clip, "s1", scene, note="manual text", auto=False)
    rerun(styled_clip, "s1", "sprites", note="again")
    _, after = _title(styled_clip)
    assert after.provenance == "manual" and after.canonical.text == "Mega"
    assert after.canonical.style == el.canonical.style


def test_pipeline_sets_matched_font_and_layout(styled_clip):
    """Task 10: the style phase matches the font on the matted alpha and stores FontGuess plus the layout that
    places the matched font's glyphs on the texture's."""
    scene, el = _title(styled_clip)
    font, style = el.canonical.font, el.canonical.style
    assert "Inter" in font.candidates and len(font.candidates) == 3 and len(font.scores) == 3
    assert font.scores == sorted(font.scores, reverse=True) and font.confidence in (0.5, 1.0)
    assert font.family_guess == font.candidates[0] and font.source == "bundled"
    assert abs(font.weight - 700) <= 100 and abs(font.size_px / 44 - 1) <= 0.03
    face = REG.face(font.family_guess, font.weight)
    w, h = int(el.canonical.width), int(el.canonical.height)
    redraw = glyph_alpha(["Sale"], font.size_px, [face], weight=font.weight, tracking_em=style.tracking_em,
                         shear_deg=style.shear_deg, box=(w, h), dx=style.dx, dy=style.dy)
    tex = cv2.imread(str(scene_dir(styled_clip, "s1") / el.canonical.texture), cv2.IMREAD_UNCHANGED)[..., 3] / 255.0
    fill = np.clip((tex - 0.0), 0, 1)
    assert np.minimum(redraw, fill).sum() / np.maximum(redraw, fill).sum() >= 0.6   # the shadow stays in the texture
    report = json.loads((scene_dir(styled_clip, "s1") / "report.json").read_text())
    assert not [m for m in report["messages"] if "font" in m]


def test_font_match_failure_keeps_preliminary_font_with_message(styled_clip, monkeypatch):
    from keepframe.fonts import match as fontmatch
    monkeypatch.setattr(fontmatch, "match_font", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    rerun(styled_clip, "s1", "sprites", note="font match fails")
    _, el = _title(styled_clip)
    assert el.canonical.font.family_guess == "sans-serif" and el.canonical.font.candidates == []
    assert el.canonical.font.confidence <= 0.5
    assert el.canonical.style is not None and el.canonical.style.tracking_em == 0 and el.canonical.style.dx == 0
    report = json.loads((scene_dir(styled_clip, "s1") / "report.json").read_text())
    assert f"{el.id}: font match failed; kept sans-serif" in report["messages"]   # R51: no exception text
    assert not [m for m in report["messages"] if "boom" in m]


def test_font_work_cap_keeps_preliminary_font_with_message(styled_clip, monkeypatch):
    """R43: a text past the scene's work cap keeps its first guess, at confidence ≤ 0.5, with a report message."""
    from keepframe.fonts import match as fontmatch
    monkeypatch.setattr(fontmatch, "SCENE_RENDERS", 0)
    rerun(styled_clip, "s1", "sprites", note="font work cap")
    _, el = _title(styled_clip)
    assert el.canonical.font.family_guess == "sans-serif" and el.canonical.font.candidates == []
    assert el.canonical.font.confidence <= 0.5
    report = json.loads((scene_dir(styled_clip, "s1") / "report.json").read_text())
    assert f"{el.id}: font not matched (scene work cap reached); kept sans-serif" in report["messages"], report["messages"]
