from refstudio.ir.synth import make_synthetic_scene
from refstudio.compose.composer import compose


def test_compose_is_self_contained_and_has_hooks(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=1)
    out = compose(scene, tmp_scene_dir, tmp_scene_dir / "composition.html")
    html = out.read_text()
    assert "gsap 3.12" in html.lower() and "CustomEase" in html
    assert 'src="data:image/png;base64,' in html
    assert "assets/" not in html.split("<script")[0]
    assert "window.__seek" in html and "window.__bbox" in html
    assert 'id="stage" data-composition-id="synth1" data-width="640" data-height="360"' in html
    for e in scene.elements:
        assert f'id="el-{e.id}"' in html
    text = [e for e in scene.elements if e.kind == "text"][0]
    assert f"<span" in html and text.canonical.text in html
    assert f'data-track-index="{int(text.z.keys[0].v)}"' in html
