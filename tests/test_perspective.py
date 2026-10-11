"""Task 3 (Stage B): one projective forward model for rx/ry on 2D elements, shared by numpy and Chromium."""
import hashlib
import re

import cv2
import numpy as np
import pytest

from keepframe.analyze.composite import composite_scene
from keepframe.ir.schema import DEFAULTS, Background, Canonical, Element, Keyframe, Scene, Track
from keepframe.ir.synth import make_texture
from keepframe.ir.tracks import affine_matrix, element_bbox, element_corners, perspective_px, projective_matrix

# (rx, ry) per frame of `_tilt_scene`; rot 10 and sx 1.2 throughout (the brief's four gate poses)
POSES = [(0, -30), (0, 20), (25, -30), (25, 20)]


def k(*pairs) -> Track:
    return Track(keys=[Keyframe(t=t, v=v) for t, v in pairs])


def _tilt_scene(sd, size=(640, 360)):
    """A white card off the stage centre (so the perspective origin matters), one gate pose per frame."""
    make_texture(sd / "assets" / "card.png", "rect", 200, 120, (255, 255, 255))
    el = Element(id="card", kind="sprite", visible=(0, 3),
                 canonical=Canonical(width=200, height=120, texture="assets/card.png"),
                 tracks={"x": k((0, 380)), "y": k((0, 160)), "rot": k((0, 10)), "sx": k((0, 1.2)),
                         "rx": k(*enumerate(rx for rx, _ in POSES)), "ry": k(*enumerate(ry for _, ry in POSES))})
    return Scene(id="tilt", size=size, fps=30, frames=4, background=Background(value="#000000"), elements=[el])


def _legacy_scene(sd):
    """Every untilted path: padding, reveal, skew, opacity, a 3D model's spin and a 3D candidate's spin."""
    rng = np.random.default_rng(7)
    (sd / "assets").mkdir(parents=True, exist_ok=True)
    for name, shape in {"a": (46, 66, 4), "b": (24, 90, 4), "c": (40, 40, 4)}.items():
        cv2.imwrite(str(sd / "assets" / f"{name}.png"), rng.integers(0, 256, shape, dtype=np.uint8))
    return Scene(id="legacy", size=(320, 180), fps=30, frames=6, background=Background(value="#203040"), elements=[
        Element(id="a", kind="text", visible=(0, 5),
                canonical=Canonical(width=60, height=40, texture="assets/a.png", texture_pad=3),
                tracks={"x": k((0, 90), (5, 150.3)), "y": k((0, 70)), "rot": k((0, -12), (5, 31.7)),
                        "sx": k((0, 0.8), (5, 1.7)), "skx": k((0, 9)), "reveal": k((0, 0.3), (5, 1))}),
        Element(id="b", kind="sprite", visible=(0, 5), z=k((0, 1)),
                canonical=Canonical(width=90, height=24, texture="assets/b.png", anchor=(0.2, 0.7)),
                tracks={"x": k((0, 200.25)), "y": k((0, 120.5), (5, 40)), "sy": k((0, 1.4)), "opacity": k((0, 0.6))}),
        Element(id="model", kind="3d", visible=(0, 5), z=k((0, 2)),
                canonical=Canonical(width=40, height=40, texture="assets/c.png", model="m.glb"),
                tracks={"x": k((0, 60)), "y": k((0, 130)), "rx": k((0, 0), (5, 40)), "ry": k((0, 70))}),
        Element(id="candidate", kind="sprite", pending_asset="3d", visible=(0, 5), z=k((0, 3)),
                canonical=Canonical(width=40, height=40, texture="assets/c.png"),
                tracks={"x": k((0, 260)), "y": k((0, 50)), "ry": k((0, 0), (5, 90))}),
    ])


def test_projective_reduces_to_affine_without_tilt():
    rng = np.random.default_rng(3)
    for _ in range(50):
        p = dict(DEFAULTS, x=rng.uniform(-500, 2500), y=rng.uniform(-500, 1500), sx=rng.uniform(0.2, 3),
                 sy=rng.uniform(0.2, 3), rot=rng.uniform(-180, 180), skx=rng.uniform(-40, 40))
        assert np.abs(projective_matrix(p, (1920, 1080)) - affine_matrix(p)).max() <= 1e-12


def test_rx_ry_signs_follow_css(tmp_path):
    scene = _tilt_scene(tmp_path)
    el = scene.elements[0]
    edge = lambda P, i, j: np.linalg.norm(P[i] - P[j])   # corners TL, TR, BR, BL
    el.tracks.update(rot=k((0, 0)), rx=k((0, 20)), ry=k((0, 0)))
    P = element_corners(el, 0, scene.size)
    assert edge(P, 0, 1) < edge(P, 3, 2) - 5        # rx > 0: the top edge leans away and is shorter
    el.tracks.update(rx=k((0, 0)), ry=k((0, 20)))
    P = element_corners(el, 0, scene.size)
    assert edge(P, 1, 2) < edge(P, 0, 3) - 5        # ry > 0: the right edge leans away and is shorter


def test_composite_tilt_draws_projected_quad(tmp_path):
    scene = _tilt_scene(tmp_path)
    for f in range(len(POSES)):
        mask = (composite_scene(scene, tmp_path, f)[..., 0] > 0.5).astype(np.uint8)
        want = element_corners(scene.elements[0], f, scene.size).astype(np.float32)
        quad = np.zeros_like(mask)   # pixels whose centre lies in the projected quad
        cv2.fillPoly(quad, [np.round((want - 0.5) * 16).astype(np.int32)], 1, shift=4)
        ys, xs = np.nonzero(mask != quad)
        # every pixel the composite and the quad disagree on sits within 1 px of the quad's outline, corners included
        err = max((abs(cv2.pointPolygonTest(want, (float(x) + 0.5, float(y) + 0.5), True)) for x, y in zip(xs, ys)), default=0.0)
        assert mask.sum() > 0.9 * quad.sum() and err <= 1.0, (POSES[f], err)


def test_composite_untilted_is_bitwise_unchanged(tmp_path):
    """sha256 of the composite at 8af72d7 (before rx/ry tilt), on the home machine's OpenCV."""
    scene = _legacy_scene(tmp_path)
    digest = hashlib.sha256(b"".join(composite_scene(scene, tmp_path, f).tobytes() for f in (0, 3, 5))).hexdigest()
    assert digest == "825786a1006129ea7707343dd76a7f56ab15cfa00dad23215510152cc4d933a1"


def test_behind_viewer_corner_skips_element(tmp_path):
    scene = _tilt_scene(tmp_path)
    el = scene.elements[0]
    el.canonical.width = 2000                         # half-width 1000·sin 80° > d = 889: the left corners have w < 0
    el.tracks = {"x": k((0, 320)), "y": k((0, 180)), "ry": k((0, 80))}
    assert not composite_scene(scene, tmp_path, 0).any()
    el.tracks["ry"] = k((0, 40))
    assert composite_scene(scene, tmp_path, 0).any()


def test_verifier_accepts_tilted_probe(tmp_path):
    from keepframe.render.renderer import RenderResult
    from keepframe.verify.verifier import LAYER_TOLERANCE_PX, verify
    scene = _tilt_scene(tmp_path)
    el, frames = scene.elements[0], list(range(len(POSES)))
    probes = [list(element_bbox(el, f, scene.size)) for f in frames]
    assert max(np.abs(np.subtract(element_bbox(el, f), b)).max() for f, b in zip(frames, probes)) > LAYER_TOLERANCE_PX
    rep = verify(scene, tmp_path, render_result=RenderResult(frames_dir=tmp_path, frames=frames, hashes=[""] * 4,
                                                             bboxes={"card": probes}))
    assert rep.passed and rep.layer_max_err_px == 0, rep


def test_lottie_refuses_tilt(tmp_path):
    from keepframe.render.lottie import preflight_lottie
    from keepframe.render.plan import PlanConflict
    scene = _tilt_scene(tmp_path)
    with pytest.raises(PlanConflict, match="rx/ry"):
        preflight_lottie(scene)
    scene.elements[0].pending_asset = "3d"            # a 3D candidate's rx/ry is its model's spin, drawn flat
    preflight_lottie(scene)


@pytest.mark.browser
def test_css_perspective_matches_numpy(tmp_path):
    from keepframe.compose.composer import compose
    from keepframe.render.renderer import load_frame, render
    scene = _tilt_scene(tmp_path)
    r = render(compose(scene, tmp_path, tmp_path / "c.html"), scene, tmp_path / "r", frames=list(range(len(POSES))))
    for i, f in enumerate(r.frames):
        a = composite_scene(scene, tmp_path, f)[..., 0] > 0.5
        b = load_frame(r.frames_dir / f"f_{i:05d}.png")[..., 0] > 0.5
        iou = (a & b).sum() / (a | b).sum()
        delta = np.abs(np.subtract(r.bboxes["card"][i], element_bbox(scene.elements[0], f, scene.size))).max()
        assert iou >= 0.97 and delta <= 1.5, (POSES[f], iou, delta)


@pytest.mark.browser
def test_untilted_stage_has_no_perspective(tmp_path):
    from playwright.sync_api import sync_playwright
    from keepframe.compose.composer import compose
    tilted = _tilt_scene(tmp_path)
    flat = tilted.model_copy(deep=True)
    del flat.elements[0].tracks["rx"], flat.elements[0].tracks["ry"]
    got = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-gpu"])
        page = browser.new_page(viewport={"width": 640, "height": 360})
        for name, scene in {"flat": flat, "tilted": tilted}.items():
            html = compose(scene, tmp_path, tmp_path / f"{name}.html")
            got[name + "_css"] = re.search(r"#stage\{[^}]*\}", html.read_text()).group()
            page.goto(html.resolve().as_uri())
            page.wait_for_function("window.__ready === true")
            got[name] = page.evaluate("() => [getComputedStyle(document.getElementById('stage')).perspective,"
                                      " document.getElementById('el-card').style.transform]")
        browser.close()
    assert "perspective" not in got["flat_css"] and "perspective" in got["tilted_css"]
    assert got["flat"] == ["none", "translate(380px, 160px) rotate(10deg) skewX(0deg) scale(1.2, 1)"]
    assert float(got["tilted"][0].removesuffix("px")) == pytest.approx(perspective_px(tilted.size), abs=0.01)
    assert got["tilted"][1] == "translate(380px, 160px) rotateX(0deg) rotateY(-30deg) rotate(10deg) skewX(0deg) scale(1.2, 1)"
