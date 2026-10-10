import cv2, numpy as np, pytest
from keepframe.analyze.composite import load_texture, texture_to_scene_affine
from keepframe.ir.colour import delta_e, srgb_to_lab
from keepframe.ir.synth import ground_truth, make_reference_scene, render_reference, true_plate
from keepframe.ir.tracks import eval_props


def _reference_layer(scene, root, el, f):
    """Straight warp of the element's own texture: the α and F the double render must give back."""
    tex = load_texture(root / el.canonical.texture)
    prem = tex.copy()
    prem[..., :3] *= prem[..., 3:4]
    p = eval_props(el, f)
    W, H = scene.size
    warped = cv2.warpAffine(prem, texture_to_scene_affine(el, p, tex.shape[:2]), (W, H), flags=cv2.INTER_LINEAR,
                            borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    a = warped[..., 3] * p["opacity"]
    fg = np.where(warped[..., 3:4] > 1e-6, warped[..., :3] / np.maximum(warped[..., 3:4], 1e-6), 0) * 255
    return a, fg


def test_double_render_recovers_exact_alpha_and_foreground(tmp_path):
    scene = make_reference_scene(tmp_path, seed=3, plate="gradient", mover=True)
    frames = [0, scene.frames // 2, scene.frames - 1]
    gt = ground_truth(scene, tmp_path, frames, renderer="numpy")
    assert set(gt) == {e.id for e in scene.elements}
    checked = 0
    for el in scene.elements:
        for f in frames:
            alpha, fg = gt[el.id][f]
            assert alpha.shape == tuple(scene.size[::-1]) and fg.shape == (*alpha.shape, 3)
            if not el.visible[0] <= f <= el.visible[1]:
                assert alpha.max() <= 1 / 255
                continue
            a_ref, f_ref = _reference_layer(scene, tmp_path, el, f)
            assert np.abs(alpha - a_ref).max() <= 1 / 255
            m = a_ref > 0.1
            if m.any():
                assert delta_e(srgb_to_lab(fg[m]), srgb_to_lab(f_ref[m])).max() <= 0.5
                checked += 1
    assert checked >= len(scene.elements)


def test_reference_scene_holds_title_60pct_and_static_logo(tmp_path):
    kinds = {}
    for plate in ("flat", "gradient", "animated", "image"):
        root = tmp_path / plate
        scene = make_reference_scene(root, seed=5, plate=plate, n_sprites=3, mover=True)
        kinds[plate] = scene.background
        titles = [e for e in scene.elements if e.kind == "text"]
        assert len(titles) == 1
        title = titles[0]
        assert title.canonical.text and title.canonical.color and title.canonical.font.family_guess != "sans-serif"
        props = [eval_props(title, f) for f in range(scene.frames)]
        held = sum(title.visible[0] <= f <= title.visible[1] and p == props[-1] and p["opacity"] == 1.0
                   for f, p in enumerate(props))
        assert held >= 0.6 * scene.frames
        logo = scene.element("logo")
        assert logo.visible == (0, scene.frames - 1)
        assert len({tuple(eval_props(logo, f).values()) for f in range(scene.frames)}) == 1
        assert sum(e.kind == "sprite" for e in scene.elements) == 3 + 1 + 1   # sprites + logo + mover
        alpha = ground_truth(scene, root, [scene.frames - 1], renderer="numpy")[title.id][scene.frames - 1][0]
        assert ((alpha > 0.02) & (alpha < 0.98)).sum() > 50                  # anti-aliased glyph edges
    assert kinds["flat"].kind == "color"
    assert kinds["gradient"].kind == "gradient" and not kinds["gradient"].gradient_keys
    assert kinds["animated"].kind == "gradient" and len(kinds["animated"].gradient_keys) >= 2
    assert kinds["image"].kind == "image" and (tmp_path / "image" / kinds["image"].value).is_file()
    a = make_reference_scene(tmp_path / "again", seed=5, plate="gradient", mover=True)
    b = make_reference_scene(tmp_path / "gradient2", seed=5, plate="gradient", mover=True)
    assert a.model_dump() == b.model_dump()                                   # seeded


def test_true_plate_and_animated_background(tmp_path):
    scene = make_reference_scene(tmp_path, seed=2, plate="animated")
    p0, p1 = (true_plate(scene, tmp_path, f, renderer="numpy") for f in (0, scene.frames - 1))
    assert p0.shape == (360, 640, 3) and np.abs(p0 - p1).mean() > 5
    video = render_reference(scene, tmp_path, tmp_path / "ref.mp4", renderer="numpy")
    from keepframe.analyze.video import read_frames
    frames, fps = read_frames(video)
    assert frames.shape == (scene.frames, 360, 640, 3) and fps == pytest.approx(30.0)


@pytest.mark.browser
def test_browser_double_render_recomposes_the_frame(tmp_path):
    from keepframe.ir.synth import render_frames
    from keepframe.ir.tracks import eval_z
    scene = make_reference_scene(tmp_path, seed=4, plate="gradient", n_sprites=2, frames=12)
    f = scene.frames - 1
    gt = ground_truth(scene, tmp_path, [f], renderer="browser")
    out = true_plate(scene, tmp_path, f, renderer="browser")
    full = render_frames(scene, tmp_path, [f], renderer="browser")[0]
    for el in sorted((e for e in scene.elements if e.visible[0] <= f <= e.visible[1]), key=lambda e: eval_z(e, f)):
        a, fg = gt[el.id][f]
        out = fg * a[..., None] + out * (1 - a[..., None])
    err = np.abs(out - full)
    assert err.mean() < 0.3 and np.percentile(err, 99.9) < 3
    assert all(gt[e.id][f][0].max() > 0.9 for e in scene.elements)
