import base64
import json
from pathlib import Path

import cv2
import numpy as np
import pytest
from pydantic import ValidationError

from keepframe.ae.spec import comp_spec, spec_asset_paths
from keepframe.analyze.composite import composite_scene
from keepframe.analyze.solid_assets import _plate
from keepframe.compose.composer import compose
from keepframe.ir.colour import delta_e, hex_to_rgb8, lab_to_srgb, rgb8_to_hex, srgb_to_lab
from keepframe.ir.gradient import gradient_at, gradient_css, gradient_t, render_gradient
from keepframe.ir.schema import (Background, Canonical, Element, Gradient, GradientKey, GradientStop, Scene,
                                 dump, load_scene_json)
from keepframe.ir.store import init_project
from keepframe.ir.synth import make_synthetic_scene
from keepframe.render.lottie import animation_from_scene, preflight_lottie
from keepframe.render.plan import PlanConflict, approve_render_plan, create_render_plan
from keepframe.session.brief import scene_brief


def stops(*cs):
    n = max(len(cs) - 1, 1)
    return [GradientStop(offset=i / n, color=c) for i, c in enumerate(cs)]


def lin(*cs, angle=180.0):
    return Gradient(kind="linear", stops=stops(*cs), angle=angle)


def scene_with(bg, size=(64, 48), frames=10):
    return Scene(id="s1", size=size, fps=30, frames=frames, background=bg, elements=[])


def png(path: Path, rgb, size=(64, 48)):
    path.parent.mkdir(parents=True, exist_ok=True)
    img = np.empty((size[1], size[0], 3), np.uint8)
    img[:] = rgb[::-1]
    cv2.imwrite(str(path), img)


OLD_JSON = json.dumps({
    "schema": "keepframe.scene/1", "id": "old", "size": [64, 48], "fps": 30, "frames": 5,
    "background": {"kind": "image", "value": "assets/plate.png", "confidence": 0.7},
    "elements": [{"id": "e1", "kind": "text", "canonical": {"width": 10, "height": 5, "text": "hi",
                  "font": {"family_guess": "Arial", "weight": 700, "size_px": 20.0, "candidates": ["A"]}},
                  "visible": [0, 4]}],
})


def test_old_scene_json_loads_and_roundtrips():
    scene = load_scene_json(OLD_JSON)
    assert scene.schema_version == "keepframe.scene/1"
    assert scene.background.kind == "image" and scene.background.confidence == 0.7
    assert (scene.background.gradient, scene.background.gradient_keys, scene.background.poster,
            scene.background.synthetic) == (None, [], None, None)
    c = scene.elements[0].canonical
    assert (c.style, c.texture_meta, c.video) == (None, None, None)
    f = c.font
    assert (f.scores, f.confidence, f.source, f.file, f.postscript, f.fallback, f.fallback_weight,
            f.fallback_scale) == ([], 1.0, "generic", None, None, None, None, 1.0)
    assert load_scene_json(dump(scene)) == scene


def test_gradient_background_validation():
    g = lin("#ffffff", "#000000")
    bg = Background(kind="gradient", value="#808080", gradient=g)
    assert bg.gradient == g
    with pytest.raises(ValidationError):
        Gradient(stops=stops("#ffffff"))                                   # 1 stop
    with pytest.raises(ValidationError):
        Gradient(stops=[GradientStop(offset=i / 8, color="#000000") for i in range(9)])
    with pytest.raises(ValidationError):
        Gradient(stops=[GradientStop(offset=0.6, color="#000000"), GradientStop(offset=0.2, color="#fff")])
    with pytest.raises(ValidationError):
        Gradient(stops=[GradientStop(offset=0, color="red"), GradientStop(offset=1, color="#ffffff")])
    with pytest.raises(ValidationError):
        Background(kind="gradient", value="#000000")                       # needs a gradient
    with pytest.raises(ValidationError):
        Background(kind="gradient", value="#000000", gradient_keys=[GradientKey(t=5, gradient=g),
                                                                    GradientKey(t=5, gradient=g)])
    bg = Background(kind="video", value="assets/plate.webm", poster="assets/plate.png")
    assert bg.poster == "assets/plate.png"
    assert load_scene_json(dump(scene_with(Background(kind="gradient", value="#808080", gradient=g)))).background.gradient == g


def test_srgb_to_lab_reference_values():
    lab = srgb_to_lab(np.array([255, 0, 0], np.uint8))
    assert lab.dtype == np.float32
    assert lab == pytest.approx([53.24, 80.09, 67.20], abs=0.05)
    assert srgb_to_lab(np.array([255, 255, 255], np.uint8)) == pytest.approx([100, 0, 0], abs=0.05)
    assert srgb_to_lab(np.array([0, 0, 0], np.uint8)) == pytest.approx([0, 0, 0], abs=0.05)
    a, b = (np.array(hex_to_rgb8(h), np.uint8) for h in ("#a8001c", "#fb9d9d"))
    assert delta_e(srgb_to_lab(a), srgb_to_lab(b)) > 30
    rng = np.random.default_rng(0).integers(0, 256, (50, 3), dtype=np.uint8)
    back = lab_to_srgb(srgb_to_lab(rng))
    assert back.dtype == np.uint8 and np.abs(back.astype(int) - rng.astype(int)).max() <= 1
    assert lab_to_srgb(np.array([50.0, 200.0, -200.0], np.float32)).dtype == np.uint8
    assert hex_to_rgb8("#AbC") == (0xAA, 0xBB, 0xCC) and rgb8_to_hex((1, 2, 255)) == "#0102ff"


def test_render_gradient_css_geometry():
    t = gradient_t(lin("#000", "#fff", angle=90), 100, 20)               # to the right
    assert t.shape == (20, 100)
    assert t[0, 0] == pytest.approx(0.005) and t[10, 99] == pytest.approx(0.995) and t[0, 0] == t[19, 0]
    t = gradient_t(lin("#000", "#fff", angle=180), 100, 20)              # to the bottom
    assert t[0, 5] == pytest.approx(0.025) and t[19, 5] == pytest.approx(0.975)
    # 45deg on a square: bottom-left corner to top-right corner
    t = gradient_t(lin("#000", "#fff", angle=45), 10, 10)
    assert t[0, 0] == pytest.approx(0.5, abs=1e-6) and t[9, 9] == pytest.approx(0.5, abs=1e-6)
    assert t[9, 0] < 0.1 and t[0, 9] > 0.9
    r = Gradient(kind="radial", stops=stops("#fff", "#000"), center=(0.5, 0.5), radius=1.0)
    t = gradient_t(r, 40, 30)
    assert t[15, 20] < 0.03 and t[0, 0] == pytest.approx(1.0, abs=0.03)
    img = render_gradient(lin("#ff0000", "#0000ff", angle=90), 100, 4)
    assert img.dtype == np.uint8 and img.shape == (4, 100, 3)
    assert img[0, 0].tolist() == [254, 0, 1] and img[0, 99].tolist() == [1, 0, 254]
    assert gradient_css(lin("#ffffff", "#f6c1d0", angle=135), 64, 48) == "linear-gradient(135deg, #ffffff 0%, #f6c1d0 100%)"
    css = gradient_css(Gradient(kind="radial", stops=stops("#fff", "#000"), center=(0.25, 0.75), radius=0.5), 200, 100)
    assert css == "radial-gradient(circle 55.9017px at 25% 75%, #ffffff 0%, #000000 100%)"


def test_gradient_at_interpolates_keys():
    a, b = lin("#000000", "#ffffff", angle=0), lin("#ff0000", "#00ff00", angle=90)
    bg = Background(kind="gradient", value="#000000", gradient=a,
                    gradient_keys=[GradientKey(t=2, gradient=a), GradientKey(t=6, gradient=b)])
    assert gradient_at(bg, 0) == a and gradient_at(bg, 2) == a and gradient_at(bg, 9) == b
    mid = gradient_at(bg, 4)
    assert mid.angle == pytest.approx(45) and mid.stops[0].color == "#800000" and mid.stops[1].color == "#80ff80"
    static = Background(kind="gradient", value="#000000", gradient=a)
    assert gradient_at(static, 7) == a


def test_composite_draws_gradient_and_animated_keys(tmp_scene_dir):
    a, b = lin("#000000", "#000000"), lin("#ffffff", "#ffffff")
    bg = Background(kind="gradient", value="#000000", gradient=a)
    img = composite_scene(scene_with(bg), tmp_scene_dir, 0)
    assert np.allclose(img, 0)
    g = lin("#ff0000", "#0000ff", angle=90)
    img = composite_scene(scene_with(Background(kind="gradient", value="#000000", gradient=g)), tmp_scene_dir, 0)
    assert img[0, 0] == pytest.approx([253 / 255, 0, 2 / 255], abs=1e-6)
    bg = Background(kind="gradient", value="#000000", gradient=a,
                    gradient_keys=[GradientKey(t=0, gradient=a), GradientKey(t=8, gradient=b)])
    s, cache = scene_with(bg), {}
    levels = [float(composite_scene(s, tmp_scene_dir, f, cache)[5, 5, 0]) for f in (0, 4, 8)]
    assert levels[0] == 0 and levels[1] == pytest.approx(0.5, abs=0.01) and levels[2] == 1
    # video background draws its poster
    png(tmp_scene_dir / "assets" / "poster.png", (10, 200, 30))
    bg = Background(kind="video", value="assets/plate.webm", poster="assets/poster.png")
    img = composite_scene(scene_with(bg), tmp_scene_dir, 3)
    assert img[7, 7] == pytest.approx([10 / 255, 200 / 255, 30 / 255], abs=1e-3)


@pytest.mark.browser
def test_css_gradient_matches_numpy(tmp_scene_dir):
    from keepframe.render.renderer import load_frame, render
    for g in (lin("#ffffff", "#f6c1d0", angle=135),
              Gradient(kind="linear", stops=stops("#102030", "#aa5500", "#ffee00"), angle=250),
              Gradient(kind="radial", stops=stops("#ffffff", "#203060"), center=(0.3, 0.6), radius=0.8)):
        scene = scene_with(Background(kind="gradient", value="#808080", gradient=g), size=(320, 180), frames=2)
        html = compose(scene, tmp_scene_dir, tmp_scene_dir / "c.html")
        r = render(html, scene, tmp_scene_dir / "r", frames=[0], probe=False)
        a = composite_scene(scene, tmp_scene_dir, 0) * 255
        b = load_frame(r.frames_dir / "f_00000.png") * 255
        d = np.abs(a - b)
        assert d.max() <= 3 and d.mean() <= 0.8, (g.kind, d.max(), d.mean())


@pytest.mark.browser
def test_animated_gradient_frames(tmp_scene_dir):
    from keepframe.render.renderer import load_frame, render
    a, b = lin("#ff0000", "#0000ff", angle=90), lin("#00ff00", "#ffff00", angle=180)
    bg = Background(kind="gradient", value="#808080", gradient=a,
                    gradient_keys=[GradientKey(t=0, gradient=a), GradientKey(t=10, gradient=b)])
    scene = scene_with(bg, size=(320, 180), frames=12)
    html = compose(scene, tmp_scene_dir, tmp_scene_dir / "c.html")
    fr = [0, 3, 7, 10, 11]
    r = render(html, scene, tmp_scene_dir / "r", frames=fr, probe=False)
    for i, f in enumerate(fr):
        x = composite_scene(scene, tmp_scene_dir, f) * 255
        y = load_frame(r.frames_dir / f"f_{i:05d}.png") * 255
        d = np.abs(x - y)
        assert d.max() <= 3 and d.mean() <= 0.8, (f, d.max(), d.mean())


def test_composer_embeds_gradient_and_video_poster(tmp_scene_dir):
    g = lin("#ffffff", "#f6c1d0", angle=135)
    s = scene_with(Background(kind="gradient", value="#808080", gradient=g))
    html = compose(s, tmp_scene_dir, tmp_scene_dir / "c.html").read_text()
    assert "linear-gradient(135deg, #ffffff 0%, #f6c1d0 100%)" in html
    png(tmp_scene_dir / "assets" / "poster.png", (1, 2, 3))
    s = scene_with(Background(kind="video", value="assets/plate.webm", poster="assets/poster.png"))
    html = compose(s, tmp_scene_dir, tmp_scene_dir / "c.html").read_text()
    assert "data:image/png;base64," in html.split("<script")[0]


def test_lottie_poster_for_static_gradient():
    g = lin("#ff0000", "#0000ff", angle=90)
    s = scene_with(Background(kind="gradient", value="#808080", gradient=g))
    preflight_lottie(s)
    anim = animation_from_scene(s, lambda raw: pytest.fail("no asset lookup for a rendered gradient"))
    layer = anim["layers"][0]
    assert layer["ty"] == 2 and layer["refId"] == "image-background"
    data = base64.b64decode(anim["assets"][0]["p"].split(",", 1)[1])
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    assert img.shape[:2] == (48, 64) and img[0, 0].tolist() == [2, 0, 253]


def test_lottie_refuses_animated_gradient_and_video():
    a, b = lin("#000000", "#ffffff"), lin("#ffffff", "#000000")
    animated = Background(kind="gradient", value="#000000", gradient=a,
                          gradient_keys=[GradientKey(t=0, gradient=a), GradientKey(t=5, gradient=b)])
    with pytest.raises(PlanConflict, match="Lottie does not animate a gradient background; export HTML or After Effects"):
        preflight_lottie(scene_with(animated))
    video = Background(kind="video", value="assets/plate.webm", poster="assets/poster.png")
    with pytest.raises(PlanConflict, match="Lottie does not animate a video background; export HTML or After Effects"):
        preflight_lottie(scene_with(video))


def test_plan_pins_poster_video_synthetic(tmp_path):
    root = tmp_path / "p1"
    base = make_synthetic_scene(root / "scenes" / "s1", seed=8, with_text=False, frames=6).model_copy(update={"id": "s1"})
    sd = root / "scenes" / "s1"
    for name in ("plate.webm", "poster.png", "synth.png"):
        (sd / "assets").mkdir(parents=True, exist_ok=True)
        (sd / "assets" / name).write_bytes(b"x" + name.encode())
    bg = Background(kind="video", value="assets/plate.webm", poster="assets/poster.png", synthetic="assets/synth.png")
    scene = base.model_copy(update={"background": bg})
    init_project(root, {"file": "source.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, 5]}, scene)
    (root / "source.mp4").write_bytes(b"source")
    (root / "meta.json").write_text(json.dumps({"id": "p1", "status": "approved", "version": "v1", "scene": "s1"}), encoding="utf-8")
    plan = create_render_plan(root, project_id="p1", scene_id="s1", version_id="v1", backend="native", mode="final")
    pinned = {a.project_path for a in plan.assets}
    assert {"scenes/s1/assets/plate.webm", "scenes/s1/assets/poster.png", "scenes/s1/assets/synth.png"} <= pinned


def test_ae_spec_gradient_and_video_use_poster_with_warning(tmp_path):
    png(tmp_path / "assets" / "poster.png", (9, 9, 9), size=(32, 24))
    video = Background(kind="video", value="assets/plate.webm", poster="assets/poster.png")
    s = scene_with(video)
    assert spec_asset_paths(s, tmp_path) == {"background.png": (tmp_path / "assets" / "poster.png").resolve()}
    spec = comp_spec(s, tmp_path, project="p", scene_id="s1", version="v1")
    bg = spec["layers"][0]
    assert bg["kind"] == "image" and bg["source"]["asset"] == "background.png"
    assert any("video" in w and "poster" in w for w in bg["warnings"]) and spec["warnings"]
    # a gradient keeps the solid layer of its mean colour and warns; a poster, when present, is used
    g = lin("#ff0000", "#0000ff")
    s = scene_with(Background(kind="gradient", value="#800080", gradient=g, poster="assets/poster.png"))
    spec = comp_spec(s, tmp_path, project="p", scene_id="s1", version="v1")
    assert spec["layers"][0]["kind"] == "image" and spec["warnings"]
    s = scene_with(Background(kind="gradient", value="#800080", gradient=g))
    spec = comp_spec(s, tmp_path, project="p", scene_id="s1", version="v1")
    assert spec["layers"][0]["kind"] == "solid" and spec["layers"][0]["source"] == {"color": "#800080"}
    assert any("gradient" in w for w in spec["warnings"])


def test_solid_assets_plate_uses_poster(tmp_path):
    sd = tmp_path
    png(sd / "assets" / "poster.png", (10, 20, 30))
    s = scene_with(Background(kind="video", value="assets/plate.webm", poster="assets/poster.png"))
    plate = _plate(s, sd)
    assert plate.shape == (48, 64, 3) and plate[0, 0].tolist() == [10, 20, 30]
    g = lin("#ff0000", "#0000ff", angle=90)
    plate = _plate(scene_with(Background(kind="gradient", value="#800080", gradient=g)), sd)
    assert plate.shape == (48, 64, 3) and plate[0, 0].tolist() == [253, 0, 2]


def test_brief_gradient_line():
    g = lin("#ffffff", "#f6c1d0", angle=135)
    text = scene_brief(scene_with(Background(kind="gradient", value="#808080", gradient=g)))
    assert "background gradient linear 135° #ffffff→#f6c1d0" in text


def test_ae_spec_video_sprite_uses_poster_with_warning(tmp_path):
    png(tmp_path / "assets" / "e1.png", (9, 90, 9), size=(20, 16))
    el = Element(id="e1", kind="sprite", visible=(0, 9),
                 canonical=Canonical(width=20, height=16, texture="assets/e1.png", video="assets/e1.video.webm"))
    s = scene_with(Background(kind="color", value="#000000")).model_copy(update={"elements": [el]})
    layer = next(l for l in comp_spec(s, tmp_path, project="p", scene_id="s1", version="v1")["layers"] if l["id"] == "kf:e1")
    assert layer["kind"] == "image" and "e1 video sprite exported as its poster image" in layer["warnings"]
