import numpy as np, pytest
from refstudio.ir.schema import Scene, Element, Canonical, Background, Keyframe, Track
from refstudio.ir.synth import make_synthetic_scene, make_texture
from refstudio.ir.tracks import element_bbox
from refstudio.compose.composer import compose
from refstudio.render.renderer import render, load_frame

pytestmark = pytest.mark.browser

def test_render_is_deterministic(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=3, with_text=False)
    html = compose(scene, tmp_scene_dir, tmp_scene_dir / "c.html")
    a = render(html, scene, tmp_scene_dir / "r1", frames=[0, 10, 30])
    b = render(html, scene, tmp_scene_dir / "r2", frames=[0, 10, 30])
    assert a.hashes == b.hashes and len(a.hashes) == 3

def test_background_pixel(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=3, with_text=False)
    html = compose(scene, tmp_scene_dir, tmp_scene_dir / "c.html")
    r = render(html, scene, tmp_scene_dir / "r", frames=[0])
    img = load_frame(r.frames_dir / "f_00000.png")
    assert img.shape == (360, 640, 3)
    assert np.allclose(img[0, 0] * 255, [0x10, 0x14, 0x18], atol=1)

def test_probe_bbox_matches_python_bbox_with_rotation_skew_scale(tmp_scene_dir):
    make_texture(tmp_scene_dir / "assets" / "e1.png", "rect", 100, 40, (255, 0, 0))
    el = Element(id="e1", kind="sprite", canonical=Canonical(width=100, height=40, texture="assets/e1.png"), visible=(0, 9),
                 tracks={"x": Track(keys=[Keyframe(t=0, v=200), Keyframe(t=9, v=400)]),
                         "y": Track(keys=[Keyframe(t=0, v=150)]), "rot": Track(keys=[Keyframe(t=0, v=33)]),
                         "skx": Track(keys=[Keyframe(t=0, v=10)]), "sx": Track(keys=[Keyframe(t=0, v=1.5)]),
                         "sy": Track(keys=[Keyframe(t=0, v=0.7)])})
    scene = Scene(id="t", size=(640, 360), fps=30, frames=10, background=Background(value="#000000"), elements=[el])
    html = compose(scene, tmp_scene_dir, tmp_scene_dir / "c.html")
    r = render(html, scene, tmp_scene_dir / "r", frames=[0, 5, 9])
    for i, f in enumerate([0, 5, 9]):
        exp = np.array(element_bbox(el, f))
        got = np.array(r.bboxes["e1"][i])
        assert np.abs(exp - got).max() < 1.5, (f, exp, got)

def test_mp4_written(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=3, frames=12, with_text=False)
    html = compose(scene, tmp_scene_dir, tmp_scene_dir / "c.html")
    r = render(html, scene, tmp_scene_dir / "r", mp4=True)
    assert r.mp4 is not None and r.mp4.stat().st_size > 1000
