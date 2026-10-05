"""Task 8b: frozen pre-optimization references and report/composite parity."""
from pathlib import Path

import cv2
import numpy as np
import pytest

from keepframe.analyze import composite, report
from keepframe.ir.paths import scene_asset_path
from keepframe.ir.schema import Background, Canonical, Element, Keyframe, Scene, Track
from keepframe.ir.tracks import element_bbox, eval_props, eval_z


def _old_composite_element(canvas, tex, A, opacity):
    H, W = canvas.shape[:2]
    prem = tex.copy()
    prem[..., :3] *= prem[..., 3:4]
    warped = cv2.warpAffine(prem, A, (W, H), flags=cv2.INTER_LINEAR,
                            borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    a = warped[..., 3:4] * opacity
    canvas *= (1.0 - a)
    canvas += warped[..., :3] * opacity


def _old_composite_scene(scene, scene_dir, f, cache=None):
    W, H = scene.size
    canvas = np.empty((H, W, 3), np.float32)
    cache = {} if cache is None else cache
    if scene.background.kind == "image":
        path = scene_asset_path(scene_dir, scene.background.value)
        key = ("background", scene.background.value, scene.size)
        bg = cache.get(key)
        if bg is None:
            bg = cache[key] = cv2.resize(composite.load_texture(path)[..., :3], (W, H))
        canvas[:] = bg
    else:
        canvas[:] = composite.hex_to_rgb(scene.background.value)
    order = sorted((e for e in scene.elements if e.visible[0] <= f <= e.visible[1]),
                   key=lambda e: eval_z(e, f))
    for el in order:
        if not el.canonical.texture:
            continue
        tex = cache.get(el.canonical.texture)
        if tex is None:
            tex = cache[el.canonical.texture] = composite.load_texture(Path(scene_dir) / el.canonical.texture)
        p = eval_props(el, f)
        reveal = float(np.clip(p["reveal"], 0.0, 1.0))
        if reveal < 1.0:
            tex = tex.copy()
            tex[:, int(round(tex.shape[1] * reveal)):, 3] = 0
        _old_composite_element(canvas, tex, composite.texture_to_scene_affine(el, p, tex.shape[:2]), p["opacity"])
    return canvas


def _old_reconstruction_error(scene, scene_dir, frames, first_frame, sample=1):
    cache = {}
    per = {}
    for f in range(0, scene.frames, sample):
        a = _old_composite_scene(scene, scene_dir, f, cache)
        b = frames[f].astype(np.float32) / 255.0
        per[f] = float(np.abs(a - b).mean())
    return {"mean_l1": float(np.mean(list(per.values()))), "per_frame": per}


def _old_element_confidence(scene, scene_dir, frames, first_frame):
    cache = {}
    out = {}
    for el in scene.elements:
        fs = list(range(el.visible[0], el.visible[1] + 1,
                        max(1, (el.visible[1] - el.visible[0] + 1) // 6)))
        l1s = []
        for f in fs:
            a = _old_composite_scene(scene, scene_dir, f, cache)
            b = frames[f].astype(np.float32) / 255.0
            x0, y0, x1, y1 = [int(round(v)) for v in element_bbox(el, f)]
            x0, y0 = max(0, x0), max(0, y0)
            x1, y1 = min(scene.size[0], x1), min(scene.size[1], y1)
            if x1 > x0 and y1 > y0:
                l1s.append(float(np.abs(a[y0:y1, x0:x1] - b[y0:y1, x0:x1]).mean()))
        out[el.id] = float(1.0 - min(1.0, np.mean(l1s) / 0.10)) if l1s else 0.0
    return out


def _track(start, end=None, last=18):
    keys = [Keyframe(t=0, v=start)]
    if end is not None:
        keys.append(Keyframe(t=last, v=end))
    return Track(keys=keys)


def _scene(directory, background="color"):
    rng = np.random.default_rng(82)
    texture = rng.integers(0, 256, (17, 23, 4), dtype=np.uint8)
    # Include a transparent edge and an opaque origin (singular affine behavior).
    texture[0, 0, 3] = 255
    texture[-1, :, 3] = 0
    assert cv2.imwrite(str(directory / "sprite.png"), cv2.cvtColor(texture, cv2.COLOR_RGBA2BGRA))
    bg = Background(value="#314159")
    if background == "image":
        plate = rng.integers(0, 256, (37, 53, 3), dtype=np.uint8)
        assert cv2.imwrite(str(directory / "plate.png"), plate)
        bg = Background(kind="image", value="plate.png")
    specifications = [
        ("moving", 70.3, 45.6, 33, 24, 0, 18, {"rot": _track(-21, 42), "sx": _track(0.7, 1.6),
          "sy": _track(1.4, 0.5), "x": _track(70.3, 98.1), "opacity": _track(0.2, 0.9),
          "reveal": _track(0, 1)}),
        ("overlap", 75.2, 49.4, 40, 32, 2, 16, {"rot": _track(17.3), "skx": _track(12.7),
          "opacity": _track(0.63), "sx": _track(-1.2), "reveal": _track(1.2, -0.2)}),
        ("left", -2.5, 12.4, 25, 35, 0, 6, {"rot": _track(27)}),
        ("bottom", 153.2, 97.7, 29, 20, 7, 18, {"rot": _track(-13), "opacity": _track(0.75)}),
        ("off", 400, -100, 23, 17, 0, 18, {}),
        # Keep the background observable despite the legacy singular warp.
        ("zero", 120, 50, 0, 0, 0, 18, {"opacity": _track(0.3)}),
        ("no-texture", 10, 10, 10, 10, 4, 4, {}),
    ]
    elements = []
    for i, (eid, x, y, w, h, first, last, tracks) in enumerate(specifications):
        elements.append(Element(id=eid, kind="sprite", visible=(first, last),
            canonical=Canonical(width=w, height=h, texture=None if eid == "no-texture" else "sprite.png"),
            tracks={"x": _track(x), "y": _track(y), **tracks},
            z=Track(keys=[Keyframe(t=0, v=i), Keyframe(t=9, v=-i)])))
    return Scene(id="report-parity", size=(160, 100), fps=30, frames=19, background=bg, elements=elements)


def _frames(scene, directory):
    rng = np.random.default_rng(83)
    cache = {}
    frames = np.stack([np.rint(_old_composite_scene(scene, directory, f, cache) * 255)
                       for f in range(scene.frames)]).astype(np.int16)
    frames += rng.integers(-3, 4, frames.shape, dtype=np.int16)
    return np.clip(frames, 0, 255).astype(np.uint8)


@pytest.mark.parametrize("background", ["color", "image"])
def test_composite_scene_matches_frozen_reference(tmp_path, background):
    scene = _scene(tmp_path, background)
    old_cache, cache = {}, {}
    for f in [*range(scene.frames), 0, 10, 5]:
        np.testing.assert_allclose(composite.composite_scene(scene, tmp_path, f, cache),
                                   _old_composite_scene(scene, tmp_path, f, old_cache), atol=1e-5, rtol=0)


@pytest.mark.parametrize("seed", range(12))
def test_composite_element_affine_rounding_and_edges(seed):
    rng = np.random.default_rng(seed)
    tex = rng.random((17, 23, 4), dtype=np.float32)
    canvas = rng.random((181, 321, 3), dtype=np.float32)
    el = Element(id="e", kind="sprite", canonical=Canonical(width=23, height=17), visible=(0, 0),
                 tracks={"x": _track(rng.uniform(-30, 350)), "y": _track(rng.uniform(-20, 200)),
                         "rot": _track(rng.uniform(-180, 180)), "sx": _track(rng.uniform(0.3, 8)),
                         "sy": _track(rng.uniform(0.3, 8)), "skx": _track(rng.uniform(-40, 40))})
    A = composite.texture_to_scene_affine(el, eval_props(el, 0), tex.shape[:2])
    expected = canvas.copy()
    _old_composite_element(expected, tex, A, 0.73)
    composite.composite_element(canvas, tex, A, 0.73)
    np.testing.assert_allclose(canvas, expected, atol=1e-5, rtol=0)


@pytest.mark.parametrize("dtype", [np.float32, np.float64])
def test_composite_element_float_affines_match_old(dtype):
    rng = np.random.default_rng(102)
    tex = rng.random((17, 23, 4), dtype=np.float32)
    canvas = rng.random((70, 1001, 3), dtype=np.float32)
    A = np.array([[1.2, 0.3, 900.3], [-0.7, 1.4, 40.7]], dtype=dtype)
    expected = canvas.copy()
    _old_composite_element(expected, tex, A, 0.8)
    composite.composite_element(canvas, tex, A, 0.8)
    np.testing.assert_allclose(canvas, expected, atol=1e-5, rtol=0)


@pytest.mark.parametrize("width,height,sx,sy", [(0, 0, 1, 1), (0, 17, 1, 1), (23, 0, 1, 1),
                                                 (23, 17, 0, 1), (23, 17, 1, 0)])
def test_degenerate_affines_preserve_old_pixels(width, height, sx, sy):
    tex = np.full((17, 23, 4), (0.4, 0.2, 0.8, 1), dtype=np.float32)
    canvas = np.full((40, 60, 3), 0.1, dtype=np.float32)
    el = Element(id="e", kind="sprite", canonical=Canonical(width=width, height=height), visible=(0, 0),
                 tracks={"x": _track(1000), "y": _track(1000), "sx": _track(sx), "sy": _track(sy)})
    A = composite.texture_to_scene_affine(el, eval_props(el, 0), tex.shape[:2])
    expected = canvas.copy()
    _old_composite_element(expected, tex, A, 0.5)
    composite.composite_element(canvas, tex, A, 0.5)
    np.testing.assert_allclose(canvas, expected, atol=1e-5, rtol=0)


@pytest.mark.parametrize("width", [1916, 1920])
@pytest.mark.parametrize("x", [1234.567, 1911.234])
def test_hd_composite_matches_old_sampling(tmp_path, width, x):
    scene = _scene(tmp_path)
    scene.size = (width, 1080)
    el = scene.elements[0]
    scene.elements = [el]
    el.tracks.update({"x": _track(x), "y": _track(1067.123), "rot": _track(31.127),
                      "sx": _track(1.371), "sy": _track(0.831), "reveal": _track(0.713)})
    np.testing.assert_allclose(composite.composite_scene(scene, tmp_path, 0),
                               _old_composite_scene(scene, tmp_path, 0), atol=1e-5, rtol=0)


@pytest.mark.parametrize("background", ["color", "image"])
@pytest.mark.parametrize("sample", [1, 4])
def test_report_metrics_match_frozen_reference(tmp_path, background, sample):
    scene = _scene(tmp_path, background)
    frames = _frames(scene, tmp_path)
    expected_rec = _old_reconstruction_error(scene, tmp_path, frames, 27, sample)
    expected_conf = _old_element_confidence(scene, tmp_path, frames, 27)
    rec, conf = report.reconstruction_and_confidence(scene, tmp_path, frames, 27, sample)
    for actual in [rec, report.reconstruction_error(scene, tmp_path, frames, 27, sample)]:
        assert actual["mean_l1"] == pytest.approx(expected_rec["mean_l1"], abs=1e-6, rel=0)
        assert actual["per_frame"] == pytest.approx(expected_rec["per_frame"], abs=1e-6, rel=0)
    assert conf == pytest.approx(expected_conf, abs=1e-6, rel=0)
    assert report.element_confidence(scene, tmp_path, frames, 27) == pytest.approx(expected_conf, abs=1e-6, rel=0)


@pytest.mark.parametrize("sample", [1, 4])
def test_combined_report_composites_each_needed_frame_once(tmp_path, monkeypatch, sample):
    scene = _scene(tmp_path)
    frames = _frames(scene, tmp_path)
    seen, caches = [], []
    original = report.composite_scene

    def counted(scene, directory, f, cache):
        seen.append(f)
        caches.append(cache)
        return original(scene, directory, f, cache)

    monkeypatch.setattr(report, "composite_scene", counted)
    report.reconstruction_and_confidence(scene, tmp_path, frames, 0, sample)
    needed = set(range(0, scene.frames, sample))
    for el in scene.elements:
        needed.update(range(el.visible[0], el.visible[1] + 1,
                            max(1, (el.visible[1] - el.visible[0] + 1) // 6)))
    assert seen == sorted(needed)
    assert all(cache is caches[0] for cache in caches)


def test_premultiplied_texture_is_cached_without_reveal_mutation(tmp_path, monkeypatch):
    scene = _scene(tmp_path)
    loads = []
    original = composite.load_texture

    def counted(path):
        loads.append(path)
        return original(path)

    monkeypatch.setattr(composite, "load_texture", counted)
    cache = {}
    composite.composite_scene(scene, tmp_path, 0, cache)
    assert ("premultiplied", "sprite.png") in cache
    tex = cache["sprite.png"]
    prem = cache[("premultiplied", "sprite.png")]
    saved_tex, saved_prem = tex.copy(), prem.copy()
    for f in [5, 18, 0, 10]:
        composite.composite_scene(scene, tmp_path, f, cache)
        assert cache[("premultiplied", "sprite.png")] is prem
    assert len(loads) == 1
    np.testing.assert_array_equal(tex, saved_tex)
    np.testing.assert_array_equal(prem, saved_prem)
    np.testing.assert_array_equal(prem[..., :3], tex[..., :3] * tex[..., 3:4])


def test_small_element_warps_only_roi_and_leaves_other_pixels(tmp_path, monkeypatch):
    scene = _scene(tmp_path)
    scene.elements = [scene.elements[2]]
    scene.elements[0].tracks["x"] = _track(80)
    shapes = []
    warp = composite.cv2.warpAffine
    remap = composite.cv2.remap

    def counted_warp(*args, **kwargs):
        result = warp(*args, **kwargs)
        shapes.append(result.shape[:2])
        return result

    def counted_remap(*args, **kwargs):
        result = remap(*args, **kwargs)
        shapes.append(result.shape[:2])
        return result

    monkeypatch.setattr(composite.cv2, "warpAffine", counted_warp)
    monkeypatch.setattr(composite.cv2, "remap", counted_remap)
    actual = composite.composite_scene(scene, tmp_path, 0)
    assert shapes and all(h * w < 160 * 100 // 4 for h, w in shapes)
    np.testing.assert_array_equal(actual[50:], np.broadcast_to(
        np.array(composite.hex_to_rgb(scene.background.value), dtype=np.float32), actual[50:].shape))


def test_off_canvas_element_does_not_warp(tmp_path, monkeypatch):
    scene = _scene(tmp_path)
    scene.elements = [scene.elements[4]]
    monkeypatch.setattr(composite.cv2, "warpAffine", lambda *a, **kw: pytest.fail("off-canvas warp"))
    monkeypatch.setattr(composite.cv2, "remap", lambda *a, **kw: pytest.fail("off-canvas remap"))
    composite.composite_scene(scene, tmp_path, 0)


def test_pipeline_finish_computes_both_metrics_in_one_pass(tmp_path, monkeypatch):
    from keepframe.analyze import pipeline

    scene = _scene(tmp_path)
    frames = _frames(scene, tmp_path)
    seen = []
    original = report.composite_scene

    def counted(scene, directory, f, cache):
        seen.append(f)
        return original(scene, directory, f, cache)

    monkeypatch.setattr(report, "composite_scene", counted)
    monkeypatch.setattr(pipeline, "group_by_motion", lambda *_: [])
    monkeypatch.setattr(pipeline, "extract_constraints", lambda *_: [])
    pipeline._finish(tmp_path, scene, frames, {}, [])
    assert seen == list(range(scene.frames))
    expected = _old_element_confidence(scene, tmp_path, frames, 0)
    assert {el.id: el.confidence for el in scene.elements} == pytest.approx(expected, abs=1e-6, rel=0)


def _benchmark():
    """Run with .venv/bin/python tests/test_report_speed.py --benchmark."""
    import json
    import tempfile
    import time

    rng = np.random.default_rng(84)
    with tempfile.TemporaryDirectory(prefix="keepframe-report-benchmark-") as folder:
        directory = Path(folder)
        elements = []
        for i in range(80):
            name = f"e{i}.png"
            tex = rng.integers(0, 256, (40, 64, 4), dtype=np.uint8)
            assert cv2.imwrite(str(directory / name), tex)
            # Stagger three-frame appearances across the full 60-frame clip.
            # Keep all 80 distinct textured elements and report the lifetimes.
            elements.append(Element(id=f"e{i}", kind="sprite", visible=(i % 58, i % 58 + 2),
                canonical=Canonical(width=64, height=40, texture=name),
                tracks={"x": _track(90 + (i % 10) * 185, 100 + (i % 10) * 185, 59),
                        "y": _track(65 + (i // 10) * 130, 75 + (i // 10) * 130, 59),
                        "rot": _track(-12 + i % 25, 12 - i % 25, 59),
                        "sx": _track(0.8, 1.2, 59), "sy": _track(1.2, 0.8, 59),
                        "opacity": _track(0.5, 0.9, 59), "reveal": _track(0.25, 1, 59)},
                z=_track(i)))
        scene = Scene(id="benchmark", size=(1920, 1080), fps=30, frames=60,
                      background=Background(value="#314159"), elements=elements)
        frames = np.empty((60, 1080, 1920, 3), np.uint8)
        cache = {}
        print("Preparing 1920x1080 / 60-frame / 80-element reference frames...", flush=True)
        for f in range(scene.frames):
            frames[f] = np.rint(composite.composite_scene(scene, directory, f, cache) * 255).astype(np.uint8)
        old_confidence_composites = sum(len(range(el.visible[0], el.visible[1] + 1,
            max(1, (el.visible[1] - el.visible[0] + 1) // 6))) for el in scene.elements)
        print(f"Timing old report stage (60 reconstruction + {old_confidence_composites} confidence composites)...", flush=True)
        start = time.perf_counter()
        old_rec = _old_reconstruction_error(scene, directory, frames, 0)
        old_conf = _old_element_confidence(scene, directory, frames, 0)
        old_seconds = time.perf_counter() - start
        print(f"Old report: {old_seconds:.6f} seconds", flush=True)
        start = time.perf_counter()
        rec, conf = report.reconstruction_and_confidence(scene, directory, frames, 0)
        new_seconds = time.perf_counter() - start
        per_error = max(abs(rec["per_frame"][f] - old_rec["per_frame"][f]) for f in rec["per_frame"])
        conf_error = max(abs(conf[eid] - old_conf[eid]) for eid in conf)
        assert abs(rec["mean_l1"] - old_rec["mean_l1"]) <= 1e-6
        assert per_error <= 1e-6 and conf_error <= 1e-6
        print(json.dumps({"scene_size": scene.size, "frames": scene.frames, "elements": len(elements),
                          "element_visible_frames": 3, "old_composites": 60 + old_confidence_composites,
                          "new_composites": 60,
                          "opencv": cv2.__version__, "opencv_threads": cv2.getNumThreads(),
                          "old_seconds": old_seconds, "new_seconds": new_seconds,
                          "speedup": old_seconds / new_seconds,
                          "mean_l1_absolute_difference": abs(rec["mean_l1"] - old_rec["mean_l1"]),
                          "max_per_frame_absolute_difference": per_error,
                          "max_confidence_absolute_difference": conf_error}, indent=2), flush=True)


if __name__ == "__main__":
    import sys

    if sys.argv[1:] != ["--benchmark"]:
        raise SystemExit("usage: .venv/bin/python tests/test_report_speed.py --benchmark")
    _benchmark()
