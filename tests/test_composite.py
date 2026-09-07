import numpy as np, pytest
from keepframe.ir.schema import Scene, Element, Canonical, Background, Keyframe, Track
from keepframe.ir.synth import make_synthetic_scene, make_texture
from keepframe.analyze.composite import composite_scene, hex_to_rgb

def test_translate_only_places_texture_exactly(tmp_scene_dir):
    make_texture(tmp_scene_dir / "assets" / "e1.png", "rect", 20, 10, (0, 255, 0))
    el = Element(id="e1", kind="sprite", canonical=Canonical(width=20, height=10, texture="assets/e1.png"), visible=(0, 0),
                 tracks={"x": Track(keys=[Keyframe(t=0, v=110)]), "y": Track(keys=[Keyframe(t=0, v=55)])})
    scene = Scene(id="t", size=(200, 100), fps=30, frames=1, background=Background(value="#000000"), elements=[el])
    img = composite_scene(scene, tmp_scene_dir, 0)
    assert img.shape == (100, 200, 3)
    assert np.allclose(img[55, 110], [0, 1, 0], atol=1e-3)     # anchor (centre) lands at (110,55)
    assert np.allclose(img[50, 100], [0, 1, 0], atol=1e-3)     # top-left corner at (100,50)
    assert np.allclose(img[49, 99], [0, 0, 0], atol=1e-3)
    assert np.allclose(img[60, 130], [0, 0, 0], atol=1e-3)

def test_hex():
    assert hex_to_rgb("#ff8000") == pytest.approx((1.0, 128 / 255, 0.0))

@pytest.mark.browser
def test_compositor_matches_browser(tmp_scene_dir):
    from keepframe.compose.composer import compose
    from keepframe.render.renderer import render, load_frame
    scene = make_synthetic_scene(tmp_scene_dir, seed=5, with_text=False)
    html = compose(scene, tmp_scene_dir, tmp_scene_dir / "c.html")
    r = render(html, scene, tmp_scene_dir / "r", frames=[0, 20, 40])
    for i, f in enumerate([0, 20, 40]):
        a = composite_scene(scene, tmp_scene_dir, f)
        b = load_frame(r.frames_dir / f"f_{i:05d}.png")
        assert np.abs(a - b).mean() < 0.02, f
