"""kf-script pages render like compose() pages (P1 Task 3)."""
import random

import numpy as np
import pytest

from keepframe.compose.composer import compose
from keepframe.ir.synth import make_reference_scene, make_synthetic_scene
from keepframe.kfscript import emit_page
from keepframe.render.renderer import load_frame, render

pytestmark = pytest.mark.browser
FRAME_MAX, MEAN_MAX = 0.002, 0.0005


def _frames(scene):
    edges = {e.visible[0] for e in scene.elements} | {min(e.visible[1] + 1, scene.frames - 1) for e in scene.elements}
    return sorted({0, scene.frames - 1, *range(0, scene.frames, 7), *edges})


def _l1s(scene, root, frames):
    a = render(compose(scene, root, root / "c.html"), scene, root / "rc", frames=frames, probe=False)
    b = render(emit_page(scene, root, root / "k.html"), scene, root / "rk", frames=frames, probe=False)
    return [float(np.abs(load_frame(a.frames_dir / f"f_{i:05d}.png") - load_frame(b.frames_dir / f"f_{i:05d}.png")).mean())
            for i in range(len(frames))]


def _assert_parity(scene, root):
    l1 = _l1s(scene, root, _frames(scene))
    assert max(l1) <= FRAME_MAX and np.mean(l1) <= MEAN_MAX, (max(l1), np.mean(l1))


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_parity_synthetic(tmp_scene_dir, seed):
    _assert_parity(make_synthetic_scene(tmp_scene_dir, seed=seed), tmp_scene_dir)


@pytest.mark.parametrize("plate", ["flat", "gradient", "animated", "image"])
def test_parity_reference_plates(tmp_scene_dir, plate):
    _assert_parity(make_reference_scene(tmp_scene_dir, 4, plate=plate, mover=True, logo=True), tmp_scene_dir)


@pytest.mark.parametrize("seed,title", [(9, "stagger"), (10, "plane")])
def test_parity_glyph_and_plane(tmp_scene_dir, seed, title):
    _assert_parity(make_reference_scene(tmp_scene_dir, seed, plate="gradient", title=title), tmp_scene_dir)


def test_parity_video(tmp_path):
    from tests.test_videoasset import _video_scene
    _assert_parity(_video_scene(tmp_path), tmp_path)


def test_seek_order_independent(tmp_scene_dir):
    scene = make_reference_scene(tmp_scene_dir, 9, plate="animated", title="stagger", mover=True)
    page = emit_page(scene, tmp_scene_dir, tmp_scene_dir / "k.html")
    frames = _frames(scene)
    shuffled = frames[:]
    random.Random(7).shuffle(shuffled)
    fwd = dict(zip(frames, render(page, scene, tmp_scene_dir / "a", frames=frames, probe=False).hashes))
    mix = dict(zip(shuffled, render(page, scene, tmp_scene_dir / "b", frames=shuffled, probe=False).hashes))
    assert fwd == mix


def test_fidelity_range_project(tmp_path):
    """A range project (source frames 3..): the page is compared with the source frames it replaces."""
    from keepframe.analyze.video import write_video
    from keepframe.ir.store import init_project_scenes, scene_dir
    from keepframe.kfscript.check import fidelity
    from keepframe.ir.synth import render_frames
    scene = make_synthetic_scene(tmp_path / "src", seed=1, frames=12)
    frames = render_frames(scene, tmp_path / "src", range(12), renderer="browser")
    lead = [np.zeros_like(frames[0])] * 3   # three black frames before the range
    clip = write_video(np.stack([f.round().clip(0, 255).astype(np.uint8) for f in lead + frames]), scene.fps, tmp_path / "clip.mp4")
    root = tmp_path / "proj"
    init_project_scenes(root, {"file": str(clip), "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [3, 14]},
                        [(scene, (3, 14), None)])
    sd = scene_dir(root, scene.id)
    import shutil
    shutil.copytree(tmp_path / "src" / "assets", sd / "assets", dirs_exist_ok=True)
    page = emit_page(scene, sd, tmp_path / "kf.html")
    res = fidelity(root, scene.id, page, every=3)
    assert res["frames"] == 4 and res["l1"] < 0.02, res
