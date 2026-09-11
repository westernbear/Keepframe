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
    active = np.full(300, 20)
    monkeypatch.setattr(R, "_cuda_mem_info", lambda: (2 * 1024 ** 3, 80 * 1024 ** 3))
    chunk, ckpt = R._frame_plan(300, 540, 960, active, "cuda")
    assert ckpt is True
    assert chunk >= 1
    monkeypatch.setattr(R, "_cuda_mem_info", lambda: (70 * 1024 ** 3, 80 * 1024 ** 3))
    fat, fat_ckpt = R._frame_plan(300, 540, 960, active, "cuda")
    assert fat == 300 and fat_ckpt is True


def test_frame_plan_ckpt_scales_with_active_sprite_count(monkeypatch):
    from keepframe.analyze import refine as R
    monkeypatch.setattr(R, "_cuda_mem_info", lambda: (16 * 1024 ** 3, 16 * 1024 ** 3))
    chunk_few, ckpt_few = R._frame_plan(200, 435, 794, np.full(200, 10), "cuda")
    chunk_many, ckpt_many = R._frame_plan(200, 435, 794, np.full(200, 200), "cuda")
    assert ckpt_few is True and ckpt_many is True
    assert chunk_many < chunk_few


def test_frame_plan_96gb_fits_checkpoint_canvases(monkeypatch):
    from keepframe.analyze import refine as R
    free = 96 * 1024 ** 3
    monkeypatch.setattr(R, "_cuda_mem_info", lambda: (free, free))
    N, h, w, n = 541, 435, 794, 219
    active = np.full(N, n)
    chunk, ckpt = R._frame_plan(N, h, w, active, "cuda")
    assert ckpt is True
    assert 1 <= chunk < N
    costs = R._frame_costs(h, w, active, True)
    prefix = np.concatenate(([0], np.cumsum(costs, dtype=np.int64)))
    assert np.max(prefix[chunk:] - prefix[:-chunk]) <= int(free * R._VRAM_FRAC)
    sparse = np.full(N, 3)
    sparse_chunk, _ = R._frame_plan(N, h, w, sparse, "cuda")
    assert sparse_chunk > chunk


def test_frame_plan_does_not_force_eight_frames_over_budget(monkeypatch):
    from keepframe.analyze import refine as R
    per = R._bytes_per_frame(540, 960, 200, True)
    free = max(1, int(per * 2 / R._VRAM_FRAC))
    monkeypatch.setattr(R, "_cuda_mem_info", lambda: (free, free))
    chunk, ckpt = R._frame_plan(20, 540, 960, np.full(20, 200), "cuda")
    assert ckpt is True
    assert 1 <= chunk < 8


@pytest.mark.skipif(not torch_available(), reason="torch not installed")
def test_refinement_samples_only_visible_sprite_frames(tmp_scene_dir, monkeypatch):
    import torch.nn.functional as F
    scene = make_synthetic_scene(tmp_scene_dir, seed=51, n_elements=2, frames=8, size=(160, 90), with_text=False, overlap=False)
    frames = np.stack([(composite_scene(scene, tmp_scene_dir, f) * 255).round().astype(np.uint8) for f in range(8)])
    raws, tex, anchors, z = _perturbed_raws(scene, tmp_scene_dir, 8)
    keys = list(raws)
    raws[keys[0]][4:, :] = np.nan
    raws[keys[1]][:5, :] = np.nan
    expected_pairs = sum(np.count_nonzero(~np.isnan(r[:, 0])) for r in raws.values())
    sampled = []
    original = F.grid_sample

    def counted(input, grid, *args, **kwargs):
        sampled.append(len(grid))
        return original(input, grid, *args, **kwargs)

    monkeypatch.setattr(F, "grid_sample", counted)
    out = refine_affine(frames, (0x10, 0x14, 0x18), raws, tex, anchors, z, iters=1, scale=0.5, device="cpu")
    assert sum(sampled) == expected_pairs
    assert expected_pairs < len(frames) * len(raws)
    for k, raw in raws.items():
        invalid = np.isnan(raw[:, 0])
        assert np.isnan(out[k][invalid]).all()


def test_refine_oom_retry_preserves_completed_chunks(monkeypatch):
    import torch
    from keepframe.analyze import refine as R
    calls = []
    failed = False

    def fake_chunk(frames, consts, raws, iters, lr, dev, H, W, h, w, checkpoint):
        nonlocal failed
        start = int(next(iter(raws.values()))[0, 0])
        calls.append((start, len(frames), checkpoint))
        if start == 3 and not failed:
            failed = True
            raise RuntimeError("CUDA out of memory")
        return {k: r + 100 for k, r in raws.items()}

    monkeypatch.setattr(R, "_refine_chunk", fake_chunk)
    monkeypatch.setattr(torch.cuda, "empty_cache", lambda: None)
    raw = np.zeros((8, 8), dtype=float)
    raw[:, 0] = np.arange(8)
    out = R._refine_chunks(np.zeros((8, 1, 1, 3), dtype=np.uint8), {}, {"s": raw}, 1, 0.1,
                           "cuda", 1, 1, 1, 1, 3, False)
    assert [call for call in calls if call[0] == 0] == [(0, 3, False)]
    assert calls[1] == (3, 3, False)
    assert calls[2] == (3, 1, True)
    assert np.array_equal(out["s"][:, 0], np.arange(8) + 100)
