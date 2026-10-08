"""Textures v2 (Task 6): matted element textures against the per-frame plate."""
import json, time
import cv2, numpy as np, pytest
from keepframe.analyze import matting
from keepframe.analyze.background import background_plate, foreground_mask_plate
from keepframe.analyze.composite import texture_to_scene_affine
from keepframe.analyze.pipeline import AnalyzeOptions, analyze_scene_frames
from keepframe.analyze.plate import PlateModel
from keepframe.analyze.text import TextBox, TextTrack, text_props
from keepframe.ir.colour import delta_e, hex_to_rgb8, srgb_to_lab
from keepframe.ir.gradient import render_gradient
from keepframe.ir.schema import (Background, Canonical, Element, FontGuess, Gradient, GradientKey, GradientStop, Keyframe, Scene,
                                 Track)
from keepframe.ir.store import current_scene, scene_dir
from keepframe.analyze import text as text_module
from keepframe.ir.synth import ground_truth, render_frames
from keepframe.ir.tracks import eval_props
from keepframe.qa.metrics import alpha_errors, bg_leak_fraction, foreground_errors, glyph_colour_delta, halo_ring

W, H = 320, 180
P = matting.PAD
RAW = ("x", "y", "sx", "sy", "rot", "skx", "sky", "opacity", "reveal")
OPTS = AnalyzeOptions(ocr=False, refine=False, use_ecc=False, generate_3d=False)


def _grad(a, b, angle=90.0):
    return Gradient(kind="linear", angle=angle, stops=[GradientStop(offset=0, color=a), GradientStop(offset=1, color=b)])


def _glyphs(text, rgb, scale=1.3, thick=3):
    """Straight RGBA glyphs with true anti-aliased edges (4× supersampled), even width and height."""
    s = 4
    (tw, th), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_DUPLEX, scale * s, thick * s)
    w, h = (tw // s + 10) // 2 * 2, ((th + base) // s + 10) // 2 * 2
    a = np.zeros((h * s, w * s), np.uint8)
    cv2.putText(a, text, (5 * s, 5 * s + th), cv2.FONT_HERSHEY_DUPLEX, scale * s, 255, thick * s, cv2.LINE_AA)
    rgba = np.zeros((h, w, 4), np.uint8)
    rgba[..., :3] = rgb
    rgba[..., 3] = np.rint(cv2.resize(a.astype(np.float32) / 255, (w, h), interpolation=cv2.INTER_AREA) * 255)
    return rgba


def _glow(size=64, sigma=10.0, peak=0.9, rgb=(255, 236, 190)):
    yy, xx = np.mgrid[:size, :size] - (size - 1) / 2
    rgba = np.zeros((size, size, 4), np.uint8)
    rgba[..., :3] = rgb
    rgba[..., 3] = np.rint(peak * np.exp(-(xx ** 2 + yy ** 2) / (2 * sigma ** 2)) * 255)
    return rgba


def _disc(d=40, rgb=(6, 214, 160), accent=(255, 209, 102), outline=None):
    s = 4
    a = np.zeros((d * s, d * s), np.uint8)
    cv2.circle(a, (d * s // 2, d * s // 2), d * s // 2 - s, 255, -1, cv2.LINE_AA)
    c = np.zeros((d * s, d * s, 3), np.uint8)
    c[:] = rgb
    cv2.circle(c, (d * s // 3, d * s // 3), d * s // 6, accent, -1, cv2.LINE_AA)
    if outline is not None:   # a rim ~2.5 px wide in its own colour, out to (beyond) the α edge
        cv2.circle(c, (d * s // 2, d * s // 2), d * s // 2 - s, outline, 5 * s, cv2.LINE_AA)
    af = a.astype(np.float32) / 255
    A = cv2.resize(af, (d, d), interpolation=cv2.INTER_AREA)
    Pm = cv2.resize(c.astype(np.float32) * af[..., None], (d, d), interpolation=cv2.INTER_AREA)
    C = np.where(A[..., None] > 1e-4, Pm / np.maximum(A, 1e-4)[..., None], 0)
    return np.dstack([C, A * 255]).round().clip(0, 255).astype(np.uint8)


def _scene(root, items, bg, n):
    """items: (id, rgba, tracks, z). Textures are written as BGRA PNGs under root/assets."""
    (root / "assets").mkdir(parents=True, exist_ok=True)
    els = []
    for eid, rgba, tracks, z in items:
        cv2.imwrite(str(root / "assets" / f"{eid}.png"), cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA))
        h, w = rgba.shape[:2]
        els.append(Element(id=eid, kind="sprite", canonical=Canonical(width=w, height=h, texture=f"assets/{eid}.png"),
                           visible=(0, n - 1), tracks=tracks, z=Track(keys=[Keyframe(t=0, v=z)])))
    return Scene(id="s", size=(W, H), fps=30, frames=n, background=bg, elements=els)


def _still(x, y):
    return {"x": Track(keys=[Keyframe(t=0, v=float(x))]), "y": Track(keys=[Keyframe(t=0, v=float(y))])}


def _frames(scene, root, sigma=1.0, seed=0, post=None):
    out = np.stack(render_frames(scene, root, range(scene.frames)))
    if post is not None:
        post(out)
    out += np.random.default_rng(seed).normal(0, sigma, out.shape).astype(np.float32)
    return np.clip(np.rint(out), 0, 255).astype(np.uint8)


def _raw(el, n):
    return np.array([[eval_props(el, f)[c] for c in RAW] for f in range(n)], np.float64)


def _plate(bg):
    img = render_gradient(bg.gradient, W, H)
    return PlateModel("gradient", img, tuple(int(v) for v in img.reshape(-1, 3).mean(0)), None, gradient=bg.gradient,
                      gradient_keys=list(bg.gradient_keys))


def _binary(frames, plate, raw, shape, f):
    """Today's binary α at frame f: foreground against the plate (ΔE > 12), in texture space."""
    h, w = shape
    M = matting.element_affine(raw[f], shape, (w, h))
    warp = lambda img: cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_REPLICATE)
    return foreground_mask_plate(warp(frames[f]), warp(plate.at(f)))


def _pad(rgba):
    return np.pad(rgba, ((P, P), (P, P), (0, 0)))


def _split(rgba):
    return rgba[..., :3].astype(np.float32), rgba[..., 3].astype(np.float32) / 255


# --- the solvers ------------------------------------------------------------------------------------------------

def test_two_colour_recovers_antialiased_glyph_alpha(tmp_path):
    rgba = _glyphs("Sale", (250, 246, 236), scale=2.0, thick=9)
    bg = Background(kind="gradient", gradient=_grad("#2b1b3d", "#c06c84", 60.0))
    scene = _scene(tmp_path, [("t", rgba, _still(160, 90), 1)], bg, 12)
    frames, plate, raw = _frames(scene, tmp_path), _plate(bg), _raw(scene.elements[0], 12)
    binary = _binary(frames, plate, raw, rgba.shape[:2], 0)
    out, meta = matting.texture_v2(raw, binary, frames, plate, kind_hint="text")
    assert meta.method == "two_colour" and meta.padding == P and out.shape[:2] == (rgba.shape[0] + 2 * P, rgba.shape[1] + 2 * P)
    (F, a), (tF, ta) = _split(out), _split(_pad(rgba))
    assert alpha_errors(a, ta)["sad"] < 0.03
    fe = foreground_errors(F, tF, a, ta)
    assert fe["f_de_interior"] < 3 and fe["f_de_edge"] < 6


def test_triangulation_recovers_soft_glow_over_varying_plate(tmp_path):
    n, rgba = 24, _glow()
    g0, g1 = _grad("#0f2027", "#2c5364", 0.0), _grad("#ffecd2", "#fcb69f", 90.0)
    bg = Background(kind="gradient", gradient=g0, gradient_keys=[GradientKey(t=0, gradient=g0), GradientKey(t=n - 1, gradient=g1)])
    scene = _scene(tmp_path, [("g", rgba, _still(160, 90), 1)], bg, n)
    frames, plate, raw = _frames(scene, tmp_path), _plate(bg), _raw(scene.elements[0], n)
    binary = _binary(frames, plate, raw, rgba.shape[:2], 0)
    out, meta = matting.texture_v2(raw, binary, frames, plate)
    assert meta.method == "triangulation"
    a, ta = out[..., 3] / 255, _pad(rgba)[..., 3] / 255
    m = (ta > 0.02) | (a > 0.02)
    assert np.abs(a - ta)[m].mean() < 0.02


def _title_case(root, n=12):
    """A held #a8001c title over a pink gradient, and today's texture: text_props against the pass-1 plate
    (median + low-pass), which bakes the held title and so keeps plate pixels around the glyphs."""
    rgba = _glyphs("Big Sale", hex_to_rgb8("#a8001c"), scale=1.4, thick=5)
    h, w = rgba.shape[:2]
    bg = Background(kind="gradient", gradient=_grad("#ffe1ea", "#ff6f91", 90.0))
    scene = _scene(root, [("t", rgba, _still(160, 90), 1)], bg, n)
    frames, plate = _frames(scene, root), _plate(bg)
    box = (160 - w // 2, 90 - h // 2, 160 + w // 2, 90 + h // 2)
    track = TextTrack(1, {f: TextBox(f, "Big Sale", box, 0.99) for f in range(n)}, "Big Sale")
    raw, old, _, _, _ = text_props(track, frames, plate.rgb, n, 0, infer_font=False, plate=background_plate(frames))
    return scene, frames, plate, raw, old, box


def test_texture_has_no_background_pixels(tmp_path):
    _, frames, plate, raw, old, (x0, y0, x1, y1) = _title_case(tmp_path)
    assert bg_leak_fraction(old, plate.image[y0:y1, x0:x1]) > 0.2
    new, meta = matting.texture_v2(raw, old[..., 3] > 127, frames, plate, kind_hint="text")
    assert bg_leak_fraction(new, plate.image[y0 - P:y1 + P, x0 - P:x1 + P]) <= 0.01
    F, a = _split(new)
    assert glyph_colour_delta(F, a, "#a8001c") < 5


def _recoloured(scene, root, eid, rgba, colour):
    """The scene with element `eid` drawn from `rgba` (canonical = its size) over a flat `colour`."""
    name = f"assets/{eid}_{rgba.shape[1]}x{rgba.shape[0]}.png"
    cv2.imwrite(str(root / name), cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA))
    els = [e.model_copy(update={"canonical": Canonical(width=rgba.shape[1], height=rgba.shape[0], texture=name)}) if e.id == eid else e
           for e in scene.elements]
    return scene.model_copy(update={"elements": els, "background": Background(kind="color", value=colour)})


def test_halo_ring_after_recolour(tmp_path):
    scene, frames, plate, raw, old, _ = _title_case(tmp_path)
    new, _ = matting.texture_v2(raw, old[..., 3] > 127, frames, plate, kind_hint="text")
    f = 6
    ideal = render_frames(scene.model_copy(update={"background": Background(kind="color", value="#1a2a6c")}), tmp_path, [f])[0]
    alpha_scene = ground_truth(scene, tmp_path, [f])["t"][f][0]
    halo = lambda rgba: halo_ring(render_frames(_recoloured(scene, tmp_path, "t", rgba, "#1a2a6c"), tmp_path, [f])[0], ideal, alpha_scene)
    assert halo(old) > 5          # today's texture: pink fringes around the glyphs on navy
    assert halo(new) < 2


def test_foreground_extended_under_zero_alpha(tmp_path):
    _, frames, plate, raw, old, _ = _title_case(tmp_path)
    new, _ = matting.texture_v2(raw, old[..., 3] > 127, frames, plate, kind_hint="text")
    F, a = _split(new)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    ring = (cv2.dilate((a >= 0.05).astype(np.uint8), k) > 0) & (a < 0.05)
    target = srgb_to_lab(np.float32(hex_to_rgb8("#a8001c")))
    assert ring.sum() > 50 and float(delta_e(srgb_to_lab(F[ring]), target).mean()) < 3
    # the helper on its own: garbage under α = 0 becomes the nearest opaque colour
    Fg = np.zeros((20, 20, 3), np.float32)
    Fg[5:15, 5:15] = (200, 40, 40)
    ag = np.zeros((20, 20), np.float32)
    ag[5:15, 5:15] = 1
    ext = matting.extend_foreground(Fg, ag)
    assert np.abs(ext - np.float32((200, 40, 40))).max() < 1e-3


def _placed(scene, root, eid, rgba):
    """The scene with element `eid` drawn from `rgba` (canonical = its size), same tracks and background."""
    name = f"assets/{eid}_v2.png"
    cv2.imwrite(str(root / name), cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA))
    els = [e.model_copy(update={"canonical": Canonical(width=rgba.shape[1], height=rgba.shape[0], texture=name)}) if e.id == eid else e
           for e in scene.elements]
    return scene.model_copy(update={"elements": els})


def test_keyed_path_recovers_alpha_and_rim_colour(tmp_path):
    """A disc with an off-centre accent and a light rim (not flat: the keyed path) held over a gradient: α in the edge
    band and F against the double-render truth; the rim keeps its own colour, unmixed from the plate (R29)."""
    n, disc = 12, _disc(48, outline=(250, 240, 225))
    bg = Background(kind="gradient", gradient=_grad("#2b1b3d", "#c06c84", 60.0))
    scene = _scene(tmp_path, [("d", disc, _still(160, 90), 1)], bg, n)
    frames, plate, raw = _frames(scene, tmp_path), _plate(bg), _raw(scene.elements[0], n)
    out, meta = matting.texture_v2(raw, _binary(frames, plate, raw, disc.shape[:2], 0), frames, plate, ref_frame=0)
    assert meta.method == "keyed"
    f = 6
    ta, tF = ground_truth(scene, tmp_path, [f])["d"][f]
    a, F = ground_truth(_placed(scene, tmp_path, "d", out), tmp_path, [f])["d"][f]
    assert alpha_errors(a, ta)["sad"] < 0.03
    fe = foreground_errors(F, tF, a, ta)
    assert fe["f_de_interior"] < 1.5 and fe["f_de_edge"] < 1.2   # the rim filled from the core colour: 1.5


def test_mistracked_frames_keep_the_canonical_frames_colour(tmp_path):
    """R28/R31: most frames carry a transform that jumped (the tracker followed something else), each a different
    way, so they do not agree among themselves either; the canonical frame wins: its colour, no plate."""
    n, disc = 12, _disc(40)
    bg = Background(kind="gradient", gradient=_grad("#ffe1ea", "#ff6f91", 90.0))
    scene = _scene(tmp_path, [("d", disc, _still(160, 90), 1)], bg, n)
    frames, plate, raw = _frames(scene, tmp_path), _plate(bg), _raw(scene.elements[0], n)
    raw[1:9, 0] += (14, -13, 0, 12, -12, 0, 13, -14)   # 12–14 px: half the solid disc lands off it
    raw[1:9, 1] += (0, 12, -13, 9, 0, -14, 12, -10)
    out, meta = matting.texture_v2(raw, disc[..., 3] > 127, frames, plate, ref_frame=0)
    assert 0 in meta.frames and not set(meta.frames) & set(range(1, 9))
    F, a = _split(out)
    tF, ta = _split(_pad(disc))
    assert bg_leak_fraction(out, plate.image[90 - 20 - P:90 + 20 + P, 160 - 20 - P:160 + 20 + P]) <= 0.01
    assert foreground_errors(F, tF, a, ta)["f_de_interior"] < 3


def test_canonical_frame_outlier_gives_way_to_consistent_frames(tmp_path):
    """R31: the canonical frame holds something no layer models (a disc crossing it mid ease-in), while the other
    frames agree among themselves: their consensus wins, and the disc stays out of the texture."""
    n, disc = 12, _disc(40)
    bg = Background(kind="gradient", gradient=_grad("#ffe1ea", "#ff6f91", 90.0))
    scene = _scene(tmp_path, [("d", disc, _still(160, 90), 1)], bg, n)
    frames, plate, raw = _frames(scene, tmp_path), _plate(bg), _raw(scene.elements[0], n)
    cv2.circle(frames[0], (152, 84), 13, (17, 138, 178), -1, cv2.LINE_AA)   # only in the canonical frame
    out, meta = matting.texture_v2(raw, disc[..., 3] > 127, frames, plate, ref_frame=0)
    assert 0 not in meta.frames and len(meta.frames) >= 6
    F, a = _split(out)
    tF, ta = _split(_pad(disc))
    assert foreground_errors(F, tF, a, ta)["f_de_interior"] < 3 and alpha_errors(a, ta)["sad"] < 0.03
    assert bg_leak_fraction(out, plate.image[90 - 20 - P:90 + 20 + P, 160 - 20 - P:160 + 20 + P]) <= 0.01


def test_two_frames_never_outvote_the_canonical_frame(tmp_path):
    """R31: a consensus needs three agreeing frames; two frames sharing the same wrong transform (a short duplicate
    track) agree with their own median trivially, and the canonical frame stands."""
    n, disc = 3, _disc(40)
    bg = Background(kind="gradient", gradient=_grad("#ffe1ea", "#ff6f91", 90.0))
    scene = _scene(tmp_path, [("d", disc, _still(160, 90), 1)], bg, n)
    frames, plate, raw = _frames(scene, tmp_path), _plate(bg), _raw(scene.elements[0], n)
    raw[1:, 0] += 14
    out, meta = matting.texture_v2(raw, disc[..., 3] > 127, frames, plate, ref_frame=0)
    assert meta.frames == [0]
    assert bg_leak_fraction(out, plate.image[90 - 20 - P:90 + 20 + P, 160 - 20 - P:160 + 20 + P]) <= 0.01


def test_keyed_uses_the_redecided_mask_after_realignment(tmp_path, monkeypatch):
    """R30: frames re-warped by the alignment get the re-decided mask too. A mask cut against the pass-1 plate keeps
    plate around the element; realign every frame (zero shift) and the keyed path must still drop that plate."""
    n, disc = 12, _disc(40)
    bg = Background(kind="gradient", gradient=_grad("#ffe1ea", "#ff6f91", 90.0))
    scene = _scene(tmp_path, [("d", disc, _still(160, 90), 1)], bg, n)
    frames, plate, raw = _frames(scene, tmp_path), _plate(bg), _raw(scene.elements[0], n)
    polluted = np.ones(disc.shape[:2], bool)          # the whole box: plate in the corners
    monkeypatch.setattr(matting, "_offsets", lambda ranked, ref, canon: {s.frame: (0.0, 0.0) for s in ranked})
    out, meta = matting.texture_v2(raw, polluted, frames, plate, ref_frame=0)
    assert meta.method == "keyed"
    F, a = _split(out)
    tF, ta = _split(_pad(disc))
    assert bg_leak_fraction(out, plate.image[90 - 20 - P:90 + 20 + P, 160 - 20 - P:160 + 20 + P]) <= 0.01
    assert alpha_errors(a, ta)["sad"] < 0.005                  # with the stale box mask: 0.027
    assert foreground_errors(F, tF, a, ta)["f_de_edge"] < 1.5


def test_mask_never_grows_into_pixels_the_region_left_out(tmp_path):
    """A navy card whose region left out a pink badge in its middle (another palette colour: another layer, or one
    the analysis missed): the matte keeps the badge out, as today's binary texture does, and solves only the rim."""
    n = 12
    card = np.zeros((40, 60, 4), np.uint8)
    card[..., :3], card[..., 3] = (20, 40, 110), 255
    card[12:28, 22:38, :3] = (255, 110, 160)
    bg = Background(kind="gradient", gradient=_grad("#e8d5b7", "#f4efe6", 90.0))
    scene = _scene(tmp_path, [("c", card, _still(160, 90), 1)], bg, n)
    frames, plate, raw = _frames(scene, tmp_path), _plate(bg), _raw(scene.elements[0], n)
    region = np.ones((40, 60), bool)
    region[12:28, 22:38] = False
    out, meta = matting.texture_v2(raw, region, frames, plate, ref_frame=0)
    a = out[P:-P, P:-P, 3] / 255
    assert a[12:28, 22:38].mean() < 0.05 and a[~(cv2.dilate((~region).astype(np.uint8), np.ones((7, 7), np.uint8)) > 0)].min() > 0.95


@pytest.mark.browser
def test_text_keeps_its_box_and_html_glyphs_match_numpy(tmp_path, monkeypatch):
    """R27: a text element keeps its unpadded canonical box, so the HTML span (left-aligned, line-height = box
    height) puts the glyphs where the numpy texture does (within 1 px)."""
    from keepframe.compose.composer import compose   # noqa: F401  (browser renderer dependency)
    truth_root, n = tmp_path / "truth", 6
    font = FontGuess(family_guess="DejaVu Sans", weight=400, size_px=40.0)
    el = Element(id="t", kind="text", role="text", visible=(0, n - 1), z=Track(keys=[Keyframe(t=0, v=1)]),
                 canonical=Canonical(width=200, height=50, text="Sale", font=font, color="#f0e0a0"), tracks=_still(160, 60))
    truth = Scene(id="s", size=(W, 120), fps=30, frames=n, background=Background(kind="color", value="#203040"), elements=[el])
    truth_root.mkdir()
    frames = np.stack([np.clip(np.rint(f), 0, 255).astype(np.uint8)
                       for f in render_frames(truth, truth_root, range(n), renderer="browser")])
    box = (60, 35, 260, 85)
    monkeypatch.setattr(text_module, "font_candidates", lambda *a: ["DejaVu Sans"])
    monkeypatch.setattr(text_module, "font_family_guess", lambda *a: "DejaVu Sans")
    root = tmp_path / "proj"
    scene = analyze_scene_frames(frames, 30, root, "s1", AnalyzeOptions(refine=False, use_ecc=False, generate_3d=False),
                                 ocr=lambda frame: [("Sale", box, 0.99)])
    (t,) = [e for e in scene.elements if e.kind == "text"]
    assert (t.canonical.width, t.canonical.height) == (200, 50) and t.canonical.texture_meta.padding == 0
    sd = scene_dir(root, "s1")
    html = render_frames(scene, sd, [3], renderer="browser")[0]
    num = render_frames(scene, sd, [3])[0]

    def ink(img):
        on = delta_e(srgb_to_lab(img), srgb_to_lab(np.float32(hex_to_rgb8("#203040")))) > 30
        ys, xs = np.nonzero(on)
        return np.array([xs.min(), ys.min(), xs.max(), ys.max()])

    assert np.abs(ink(html) - ink(num)).max() <= 1, (ink(html), ink(num))


# --- frame choice ---------------------------------------------------------------------------------------------

def test_frame_scoring_skips_blurred_partial_occluded_and_faded_frames(tmp_path):
    """Frames 0–3 run off the left edge, 4–7 fade, 8–11 sit under another layer, 12–15 race past motion-blurred;
    16–29 are clean and slow. Only clean frames may be picked."""
    n, disc = 30, _disc()
    xs = [0.0, 4, 8, 12] + [90, 92, 94, 96] + [150, 151, 152, 153] + [180, 200, 220, 240] + [100 + 1.0 * i for i in range(14)]
    tracks = {"x": Track(keys=[Keyframe(t=f, v=float(x)) for f, x in enumerate(xs)]), "y": Track(keys=[Keyframe(t=0, v=90.0)]),
              "opacity": Track(keys=[Keyframe(t=f, v=0.5 if 4 <= f <= 7 else 1.0) for f in range(n)])}
    box = np.zeros((30, 30, 4), np.uint8)
    box[..., :3], box[..., 3] = (60, 60, 60), 255
    cover = {"x": Track(keys=[Keyframe(t=0, v=-200.0), Keyframe(t=7, v=-200.0), Keyframe(t=8, v=155.0), Keyframe(t=11, v=155.0),
                              Keyframe(t=12, v=-200.0)]), "y": Track(keys=[Keyframe(t=0, v=90.0)])}
    bg = Background(kind="gradient", gradient=_grad("#355c7d", "#c06c84", 90.0))
    scene = _scene(tmp_path, [("d", disc, tracks, 1), ("c", box, cover, 2)], bg, n)

    def blur(out):
        k = np.full((1, 15), 1 / 15, np.float32)
        for f in range(12, 16):
            out[f] = cv2.filter2D(out[f], -1, k)

    frames, plate, raw = _frames(scene, tmp_path, post=blur), _plate(bg), _raw(scene.elements[0], n)
    cr = _raw(scene.elements[1], n)
    prem = box.astype(np.float32) / 255

    def others_at(f):
        if not 8 <= f <= 11:
            return []
        return [matting.Layer(prem, matting.element_affine(cr[f], (30, 30), (30, 30)), 1.0, True)]

    samples = matting.gather_samples(raw, disc[..., 3] > 127, frames, plate, others_at)
    top, relaxed = matting.score_samples(samples, raw, top_k=8)
    assert relaxed == [] and len(top) == 8
    assert all(16 <= s.frame for s in top), [s.frame for s in top]
    by = {s.frame: s for s in samples}
    assert all(by[f].visibility < 0.98 for f in range(4)) and all(by[f].opacity < 0.95 for f in range(4, 8))
    assert all(by[f].occlusion > 0.02 for f in range(8, 12))
    assert max(by[f].score for f in range(12, 16)) < min(s.score for s in top)


# --- geometry, pipeline, fail-soft, speed -----------------------------------------------------------------------

def test_texture_padding_keeps_transform(tmp_path):
    rng = np.random.default_rng(3)
    for _ in range(20):
        row = np.array([rng.uniform(0, 300), rng.uniform(0, 200), rng.uniform(0.5, 2), rng.uniform(0.5, 2), rng.uniform(-90, 90),
                        rng.uniform(-20, 20), 0.0, 1.0, 1.0])
        w, h = int(rng.integers(10, 90)), int(rng.integers(10, 90))
        el = Element(id="e", kind="sprite", canonical=Canonical(width=w, height=h), visible=(0, 0))
        props = dict(zip(RAW, row))
        assert np.allclose(matting.element_affine(row, (h, w), (w, h)), texture_to_scene_affine(el, props, (h, w)))
        A = matting.element_affine(row, (h, w), (w, h))
        B = matting.element_affine(row, (h + 2 * P, w + 2 * P), (w + 2 * P, h + 2 * P))
        pts = np.array([[0, 0, 1], [w - 1, 0, 1], [3.5, h - 2, 1]], np.float64).T
        assert np.allclose(A @ pts, B @ (pts + [[P], [P], [0]]))
    # a reveal edge stays where it was on the padded texture
    raw = np.array([[10, 10, 1, 1, 0, 0, 0, 1, 0.25], [10, 10, 1, 1, 0, 0, 0, 1, 1.0]])
    matting.pad_reveal(raw, 100, P)
    assert raw[0, 8] * (100 + 2 * P) == pytest.approx(P + 25) and raw[1, 8] == 1.0
    # through the pipeline: the canonical grows by 2p, the texture lands where the element is
    n, disc = 16, _disc()
    tracks = {"x": Track(keys=[Keyframe(t=0, v=60.0), Keyframe(t=n - 1, v=260.0)]), "y": Track(keys=[Keyframe(t=0, v=90.0)])}
    bg = Background(kind="gradient", gradient=_grad("#355c7d", "#c06c84", 90.0))
    truth = _scene(tmp_path / "truth", [("d", disc, tracks, 1)], bg, n)
    frames = _frames(truth, tmp_path / "truth", sigma=0.5)
    root = tmp_path / "proj"
    scene = analyze_scene_frames(frames, 30, root, "s1", OPTS)
    props = __import__("pickle").loads((scene_dir(root, "s1") / "stages" / "props.pkl").read_bytes())
    (key, p), = [(k, v) for k, v in props.items() if not k.startswith("_")]
    el = scene.element(json.loads((scene_dir(root, "s1") / "stages" / "ids.json").read_text())[key])
    meta = el.canonical.texture_meta
    assert meta is not None and meta.padding == P and meta.method in ("triangulation", "two_colour", "keyed")
    tex = cv2.imread(str(scene_dir(root, "s1") / el.canonical.texture), cv2.IMREAD_UNCHANGED)
    assert (el.canonical.width, el.canonical.height) == (tex.shape[1], tex.shape[0]) == p["canon"].shape[1::-1]
    gt = ground_truth(truth, tmp_path / "truth", [8])["d"][8][0]
    got = ground_truth(scene, scene_dir(root, "s1"), [8])[el.id][8][0]
    ys, xs = np.mgrid[:H, :W]
    centroid = lambda a: np.array([(a * xs).sum(), (a * ys).sum()]) / a.sum()
    assert np.abs(centroid(got) - centroid(gt)).max() < 1.0


def test_failure_falls_back_to_binary_with_message(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(matting, "texture_v2", boom)
    n, disc = 12, _disc()
    tracks = {"x": Track(keys=[Keyframe(t=0, v=80.0), Keyframe(t=n - 1, v=220.0)]), "y": Track(keys=[Keyframe(t=0, v=90.0)])}
    bg = Background(kind="gradient", gradient=_grad("#355c7d", "#c06c84", 90.0))
    truth = _scene(tmp_path / "truth", [("d", disc, tracks, 1)], bg, n)
    root = tmp_path / "proj"
    scene = analyze_scene_frames(_frames(truth, tmp_path / "truth", sigma=0.5), 30, root, "s1", OPTS)
    (el,) = scene.elements
    meta = el.canonical.texture_meta
    assert meta.method == "binary" and meta.padding == 0 and meta.confidence < 0.5
    tex = cv2.imread(str(scene_dir(root, "s1") / el.canonical.texture), cv2.IMREAD_UNCHANGED)
    assert set(np.unique(tex[..., 3])) <= {0, 255} and el.canonical.width == tex.shape[1]
    messages = json.loads((scene_dir(root, "s1") / "report.json").read_text())["messages"]
    assert any(m.startswith(f"{el.id}: matted texture failed") and "boom" in m for m in messages), messages


def test_textures_phase_failure_keeps_every_binary_texture(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("index exploded")

    monkeypatch.setattr(matting, "_layers", boom)
    n, disc = 12, _disc()
    tracks = {"x": Track(keys=[Keyframe(t=0, v=80.0), Keyframe(t=n - 1, v=220.0)]), "y": Track(keys=[Keyframe(t=0, v=90.0)])}
    bg = Background(kind="gradient", gradient=_grad("#355c7d", "#c06c84", 90.0))
    truth = _scene(tmp_path / "truth", [("d", disc, tracks, 1)], bg, n)
    root = tmp_path / "proj"
    scene = analyze_scene_frames(_frames(truth, tmp_path / "truth", sigma=0.5), 30, root, "s1", OPTS)
    (el,) = scene.elements
    assert el.canonical.texture_meta is None
    messages = json.loads((scene_dir(root, "s1") / "report.json").read_text())["messages"]
    assert "textures v2 skipped: RuntimeError: index exploded" in messages


def test_mover_filled_pixels_stay_opaque(tmp_path):
    """R23: a mover's pixels filled in under a text box are kept opaque with their filled colour."""
    n, disc = 8, _disc(48)
    bg = Background(kind="gradient", gradient=_grad("#355c7d", "#c06c84", 90.0))
    scene = _scene(tmp_path, [("d", disc, _still(160, 90), 1)], bg, n)
    frames, plate, raw = _frames(scene, tmp_path), _plate(bg), _raw(scene.elements[0], n)
    hidden = np.zeros(disc.shape[:2], bool)
    hidden[20:28, 10:38] = True
    rgb = disc[..., :3].copy()
    rgb[hidden] = (1, 2, 3)
    out, _ = matting.texture_v2(raw, disc[..., 3] > 127, frames, plate, kind_hint="mover", keep=(hidden, rgb))
    inner = np.pad(hidden, P)
    assert (out[..., 3][inner] == 255).all() and (out[..., :3][inner] == (1, 2, 3)).all()


def test_textures_v2_speed():
    """40 elements of 200×100 over 150 frames (in memory), through the pipeline's phase: < 20 s."""
    n, W2, H2 = 150, 640, 360
    plate_img = render_gradient(_grad("#0f2027", "#c06c84", 60.0), W2, H2)
    frames = np.repeat(plate_img[None], n, 0)
    rng = np.random.default_rng(1)
    props = {}
    tex = np.zeros((100, 200, 4), np.uint8)
    tex[..., :3] = (230, 120, 60)
    tex[10:90, 10:190, 3] = 255
    for i in range(40):
        x0, y0, vx = rng.uniform(110, 530), rng.uniform(60, 300), rng.uniform(-1, 1)
        raw = np.full((n, 8), np.nan)
        for f in range(n):
            x = int(round(x0 + vx * f))
            raw[f] = [x, int(y0), 1, 1, 0, 0, 0, 1]
            if 100 <= x <= W2 - 100:
                frames[f, int(y0) - 40:int(y0) + 40, x - 90:x + 90] = (230, 120, 60)
        props[f"o{i}"] = {"raw": raw, "canon": tex, "cf": 0, "kind": "sprite", "z": i + 1, "first": 0, "last": n - 1}
    model = PlateModel("gradient", plate_img, (100, 80, 90), None)
    t0 = time.perf_counter()
    matting.matte_props(props, frames, model, workers=4)
    secs = time.perf_counter() - t0
    assert all(p.get("texture_meta") for p in props.values())
    assert secs < 20, secs
