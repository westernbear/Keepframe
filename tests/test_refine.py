import numpy as np, pytest
from keepframe.ir.synth import make_synthetic_scene
from keepframe.ir.tracks import eval_props
from keepframe.analyze.composite import composite_scene, load_texture
from keepframe.analyze.refine import refine_affine, torch_available

pytestmark = pytest.mark.gpu

@pytest.mark.skipif(not torch_available(), reason="torch not installed")
def test_refinement_reduces_position_error(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=51, n_elements=2, frames=8, size=(160, 90), with_text=False, overlap=False)
    frames = np.stack([(composite_scene(scene, tmp_scene_dir, f) * 255).round().astype(np.uint8) for f in range(8)])
    raws, tex, anchors, z = {}, {}, {}, {}
    for e in scene.elements:
        r = np.array([[eval_props(e, f)[k] for k in ("x", "y", "sx", "sy", "rot", "skx", "sky", "opacity")] for f in range(8)])
        r[:, 0] += 3.0; r[:, 1] -= 2.0                      # perturb
        raws[e.id] = r; tex[e.id] = (load_texture(tmp_scene_dir / e.canonical.texture) * 255).astype(np.uint8)
        anchors[e.id] = e.canonical.anchor; z[e.id] = int(e.z.keys[0].v)
    out = refine_affine(frames, (0x10, 0x14, 0x18), raws, tex, anchors, z, iters=80, scale=1.0, device="cpu")
    for e in scene.elements:
        gt = np.array([[eval_props(e, f)["x"], eval_props(e, f)["y"]] for f in range(8)])
        before = np.abs(raws[e.id][:, :2] - gt).mean(); after = np.abs(out[e.id][:, :2] - gt).mean()
        assert after < before and after < 1.0, (e.id, before, after)


def _perturbed_raws(scene, tmp_scene_dir, n):
    raws, tex, anchors, z = {}, {}, {}, {}
    for e in scene.elements:
        r = np.array([[eval_props(e, f)[k] for k in ("x", "y", "sx", "sy", "rot", "skx", "sky", "opacity")] for f in range(n)])
        r[:, 0] += 3.0; r[:, 1] -= 2.0
        raws[e.id] = r; tex[e.id] = (load_texture(tmp_scene_dir / e.canonical.texture) * 255).astype(np.uint8)
        anchors[e.id] = e.canonical.anchor; z[e.id] = int(e.z.keys[0].v)
    return raws, tex, anchors, z


@pytest.mark.skipif(not torch_available(), reason="torch not installed")
def test_refinement_downscales_on_cpu(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=51, n_elements=2, frames=8, size=(160, 90), with_text=False, overlap=False)
    frames = np.stack([(composite_scene(scene, tmp_scene_dir, f) * 255).round().astype(np.uint8) for f in range(8)])
    raws, tex, anchors, z = _perturbed_raws(scene, tmp_scene_dir, 8)
    out = refine_affine(frames, (0x10, 0x14, 0x18), raws, tex, anchors, z, iters=40, scale=0.5, device="cpu")
    for e in scene.elements:
        assert out[e.id].shape == raws[e.id].shape
        valid = ~np.isnan(raws[e.id][:, 0])
        assert np.isfinite(out[e.id][valid]).all()


@pytest.mark.skipif(not torch_available(), reason="torch not installed")
def test_refinement_chunks_frames(tmp_scene_dir, monkeypatch):
    from keepframe.analyze import refine as R
    monkeypatch.setattr(R, "_frame_plan", lambda N, h, w, n_sprites, dev: (3, False))
    scene = make_synthetic_scene(tmp_scene_dir, seed=51, n_elements=2, frames=8, size=(160, 90), with_text=False, overlap=False)
    frames = np.stack([(composite_scene(scene, tmp_scene_dir, f) * 255).round().astype(np.uint8) for f in range(8)])
    raws, tex, anchors, z = _perturbed_raws(scene, tmp_scene_dir, 8)
    out = refine_affine(frames, (0x10, 0x14, 0x18), raws, tex, anchors, z, iters=80, scale=1.0, device="cpu")
    for e in scene.elements:
        gt = np.array([[eval_props(e, f)["x"], eval_props(e, f)["y"]] for f in range(8)])
        before = np.abs(raws[e.id][:, :2] - gt).mean(); after = np.abs(out[e.id][:, :2] - gt).mean()
        assert after < before and after < 1.0, (e.id, before, after)


@pytest.mark.skipif(not torch_available(), reason="torch not installed")
def test_refinement_checkpoint_reduces_position_error(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=51, n_elements=2, frames=8, size=(160, 90), with_text=False, overlap=False)
    frames = np.stack([(composite_scene(scene, tmp_scene_dir, f) * 255).round().astype(np.uint8) for f in range(8)])
    raws, tex, anchors, z = _perturbed_raws(scene, tmp_scene_dir, 8)
    out = refine_affine(frames, (0x10, 0x14, 0x18), raws, tex, anchors, z, iters=80, scale=1.0, device="cpu", checkpoint=True)
    for e in scene.elements:
        gt = np.array([[eval_props(e, f)["x"], eval_props(e, f)["y"]] for f in range(8)])
        before = np.abs(raws[e.id][:, :2] - gt).mean(); after = np.abs(out[e.id][:, :2] - gt).mean()
        assert after < before and after < 1.0, (e.id, before, after)


def test_frame_plan_uses_checkpoint_when_full_graph_does_not_fit(monkeypatch):
    from keepframe.analyze import refine as R
    monkeypatch.setattr(R, "_cuda_mem_info", lambda: (2 * 1024 ** 3, 80 * 1024 ** 3))
    chunk, ckpt = R._frame_plan(300, 540, 960, 20, "cuda")
    assert ckpt is True
    assert chunk > 1
    monkeypatch.setattr(R, "_cuda_mem_info", lambda: (70 * 1024 ** 3, 80 * 1024 ** 3))
    fat, fat_ckpt = R._frame_plan(300, 540, 960, 20, "cuda")
    assert fat == 300 and fat_ckpt is True


def test_frame_plan_96gb_does_not_halve_from_overclaimed_chunk(monkeypatch):
    """219 sprites at 435x794x541 used to plan chunk=541 with per-sprite canvas checkpoints (~200GiB)."""
    from keepframe.analyze import refine as R
    monkeypatch.setattr(R, "_cuda_mem_info", lambda: (96 * 1024 ** 3, 96 * 1024 ** 3))
    chunk, ckpt = R._frame_plan(541, 435, 794, 219, "cuda")
    assert ckpt is True
    assert chunk == 541


def test_refine_checkpoint_composites_once():
    import inspect
    from keepframe.analyze.refine import _refine_chunk
    src = inspect.getsource(_refine_chunk)
    assert "ckpt(stamp" not in src
    assert "ckpt(compose" in src or "checkpoint(compose" in src
