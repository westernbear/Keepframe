import json

import cv2
import numpy as np
import pytest

from keepframe.analyze.pipeline import AnalyzeOptions, analyze_scene_frames
from keepframe.analyze.solids import Solid, _change, estimate_spin, find_solids
from keepframe.ir.synth import make_spinning_sphere_video


def sphere_inputs():
    frames = make_spinning_sphere_video()
    fg = np.any(frames != (16, 20, 24), axis=-1)
    return frames, fg, np.zeros_like(fg)


def test_spinning_sphere_is_one_solid_and_translation_does_not_bias_spin():
    frames, fg, text = sphere_inputs()
    solids = find_solids(frames, fg, [], text)
    assert len(solids) == 1
    solid = solids[0]
    masks = [solid.frames[f][1] for f in range(len(frames))]
    boxes = [solid.frames[f][0] for f in range(len(frames))]
    rx, ry = estimate_spin(frames, masks, boxes)
    assert np.polyfit(np.arange(len(ry)), ry, 1)[0] == pytest.approx(6, abs=1.5)
    assert np.max(np.abs(rx)) < 2


def test_rigid_gradient_card_is_not_solid():
    frames = np.zeros((30, 140, 200, 3), np.uint8)
    fg = np.zeros(frames.shape[:3], bool)
    card = np.broadcast_to(np.linspace(40, 240, 72, dtype=np.uint8)[None, :, None], (48, 72, 3))
    for f in range(len(frames)):
        frames[f, 40:88, 20 + f:92 + f] = card
        fg[f, 40:88, 20 + f:92 + f] = True
    assert find_solids(frames, fg, [], np.zeros_like(fg)) == []


@pytest.mark.parametrize("widths,expected", [([100] * 30, 0), ([61] * 30, 0),
                                          ([60] * 30, 1), ([61] * 14 + [60] * 16, 1)])
def test_changing_blob_uses_median_bbox_area_limit(widths, expected):
    frames = np.zeros((30, 100, 100, 3), np.uint8)
    fg = np.zeros(frames.shape[:3], bool)
    rng = np.random.default_rng(4)
    for f, width in enumerate(widths):
        frames[f, :, :width] = rng.integers(30, 256, (100, width, 3), dtype=np.uint8)
        fg[f, :, :width] = True
    assert len(find_solids(frames, fg, [], np.zeros_like(fg))) == expected


def test_moving_flat_rectangle_and_text_are_not_solids():
    frames = np.zeros((30, 120, 180, 3), np.uint8)
    fg = np.zeros(frames.shape[:3], bool)
    for f in range(len(frames)):
        frames[f, 30:70, 10 + f:50 + f] = 200
        fg[f, 30:70, 10 + f:50 + f] = True
    assert find_solids(frames, fg, [], np.zeros_like(fg)) == []
    sphere, sphere_fg, _ = sphere_inputs()
    assert find_solids(sphere, sphere_fg, [], sphere_fg) == []


def test_translating_flat_rectangle_with_opacity_changes_is_not_solid():
    frames = np.zeros((30, 120, 180, 3), np.uint8)
    fg = np.zeros(frames.shape[:3], bool)
    for f in range(len(frames)):
        frames[f, 30:70, 10 + f:50 + f] = 100 + 3 * f
        fg[f, 30:70, 10 + f:50 + f] = True
    assert find_solids(frames, fg, [], np.zeros_like(fg)) == []


def test_analysis_merges_sphere_and_preserves_fragments_and_spin(tmp_path):
    frames, _, _ = sphere_inputs()
    scene = analyze_scene_frames(frames, 30, tmp_path, "s1", AnalyzeOptions(
        bg_override="#101418", ocr=False, refine=False, use_ecc=False, generate_3d=False))
    solids = [e for e in scene.elements if e.pending_asset == "3d"]
    assert len(solids) == 1
    el = solids[0]
    assert el.kind == "sprite" and el.pending_asset == "3d"
    assert "ry" in el.tracks and "reveal" not in el.tracks
    sd = tmp_path / "scenes" / "s1"
    raw = np.load(sd / el.raw)
    assert raw["raw"].shape[1] == 11
    assert np.isnan(raw["raw"][:, 8]).all()
    assert tuple(raw["cols"])[9:] == ("rx", "ry")
    import pickle
    props = pickle.loads((sd / "stages" / "props.pkl").read_bytes())
    solid = next(p for k, p in props.items() if not k.startswith("_") and "fragments" in p)
    assert solid["fragments"]
    saved = pickle.loads((sd / "stages" / "solids.pkl").read_bytes())
    for candidate in saved:
        for (x0, y0, x1, y1), mask in candidate.frames.values():
            assert mask.shape == (y1 - y0, x1 - x0)
    assert len(scene.elements) < len(solid["fragments"])
    assert "3D 후보 1개" in json.loads((sd / "report.json").read_text())["messages"]


def test_fragment_lifetime_and_membership_thresholds():
    from keepframe.analyze.regions import Region
    from keepframe.analyze.tracking import ObjectTrack

    frames = np.zeros((30, 80, 80, 3), np.uint8)
    frames[:, 20:60, 20:60] = 200
    fg = np.any(frames != 0, axis=-1)

    def track(tid, indices, centre=(40, 40)):
        return ObjectTrack(id=tid, regions={f: Region(f, 0, (200, 200, 200), (20, 20, 60, 60), 1600,
                                                      centre, np.ones((40, 40), bool)) for f in indices})

    short = [track(i, range(i * 10, (i + 1) * 10)) for i in range(3)]
    solids = find_solids(frames, fg, short, np.zeros_like(fg))
    assert len(solids) == 1 and solids[0].members == [0, 1, 2]
    assert find_solids(frames, fg, [track(i, range(15)) for i in range(3)], np.zeros_like(fg)) == []
    outside = short[:2] + [track(2, range(20, 30), (10, 10))]
    assert find_solids(frames, fg, outside, np.zeros_like(fg)) == []


def test_scene_lifetime_floor_and_missing_spin_masks():
    frames, fg, text = sphere_inputs()
    assert find_solids(frames[:11], fg[:11], [], text[:11]) == []
    padded = np.zeros((110, *frames.shape[1:]), np.uint8)
    padded[:len(frames)] = frames
    masks = np.zeros(padded.shape[:3], bool)
    masks[:len(frames)] = fg
    assert find_solids(padded, masks, [], np.zeros_like(masks)) == []
    rx, ry = estimate_spin(frames[:3], [fg[0], None, fg[2]], [(0, 0, 10, 10), None, (0, 0, 10, 10)])
    assert np.array_equal(rx, [0, 0, 0]) and np.array_equal(ry, [0, 0, 0])


def test_merged_solid_keeps_source_mask_in_analysis_overlay(tmp_path, monkeypatch):
    from keepframe.ir.store import init_project, current_scene
    from keepframe.review.overlay import snapshot_from_stages, frame_overlay

    monkeypatch.setattr("keepframe.analyze.solid_assets.solid_errors",
                        lambda *_a, **_k: {"fragments": 0.2, "still": 0.1, "model": None})
    scene = analyze_scene_frames(make_spinning_sphere_video(frames=12), 30, tmp_path, "s1", AnalyzeOptions(
        bg_override="#101418", ocr=False, refine=False, use_ecc=False, generate_3d=False))
    sd = tmp_path / "scenes" / "s1"
    init_project(tmp_path, {"file": "unused.mp4", "fps": 30, "size": [240, 180]}, scene,
                 analysis_file=snapshot_from_stages(sd, scene))
    _, version = current_scene(tmp_path, "s1")
    solid = next(e for e in scene.elements if e.pending_asset == "3d")
    overlay = frame_overlay(tmp_path, scene, version, 0)
    observation = next(o for o in overlay["objects"] if o["id"] == solid.id)
    assert observation["regions"][0]["source"] == "mask"
    assert observation["regions"][0]["rings"]


def test_thirty_moving_blobs_at_720p_finish_under_ten_seconds():
    import pickle
    import time

    frames = np.zeros((60, 720, 1280, 3), np.uint8)
    fg = np.zeros(frames.shape[:3], bool)
    rng = np.random.default_rng(15)
    for f in range(60):
        for i in range(30):
            x, y = 30 + (i % 10) * 120 + f, 50 + (i // 10) * 200
            fg[f, y:y + 24, x:x + 24] = True
            frames[f, y:y + 24, x:x + 24] = rng.integers(30, 256, (24, 24, 3), dtype=np.uint8)
    started = time.perf_counter()
    solids = find_solids(frames, fg, [], np.zeros_like(fg))
    elapsed = time.perf_counter() - started
    assert elapsed < 10, f"blob tracking took {elapsed:.3f}s"
    assert len(solids) == 30
    assert all(len(s.frames) == 60 for s in solids)
    assert all(m.shape == (b[3] - b[1], b[2] - b[0]) for s in solids for b, m in s.frames.values())
    assert len(pickle.dumps(solids)) < 2_000_000


def test_two_thousand_tiny_components_are_skipped_before_tracking(monkeypatch):
    import time
    import keepframe.analyze.solids as module

    frames = np.zeros((12, 720, 1280, 3), np.uint8)
    sphere = make_spinning_sphere_video(frames=12)
    frames[:, :180, 950:1190] = sphere
    fg = np.zeros(frames.shape[:3], bool)
    fg[:, :180, 950:1190] = np.any(sphere != (16, 20, 24), axis=-1)
    for row in range(40):
        for col in range(50):
            y, x = 200 + row * 12, 10 + col * 12
            fg[:, y, x] = True
    count, *_ = cv2.connectedComponentsWithStats(cv2.dilate(fg[0].astype(np.uint8), np.ones((11, 11), np.uint8)))
    assert count == 2002  # Background, 2,000 isolated noise components, and the sphere.
    allocations = 0
    def candidate():
        nonlocal allocations
        allocations += 1
        assert allocations == 1, "tiny components entered the tracker"
        return Solid()
    monkeypatch.setattr(module, "Solid", candidate)
    started = time.perf_counter()
    solids = module.find_solids(frames, fg, [], np.zeros_like(fg))
    elapsed = time.perf_counter() - started
    assert elapsed < 10, f"noisy frame tracking took {elapsed:.3f}s"
    assert len(solids) == 1 and len(solids[0].frames) == 12
    assert all(box[0] >= 950 for box, _ in solids[0].frames.values())


def test_legacy_solid_cache_is_rewritten_with_cropped_masks(tmp_path):
    import pickle
    from keepframe.analyze.pipeline import _pk

    (tmp_path / "stages").mkdir()
    mask = np.zeros((720, 1280), bool)
    mask[50:70, 30:60] = True
    solid = Solid(frames={0: ((30, 50, 60, 70), mask)})
    path = tmp_path / "stages/solids.pkl"
    path.write_bytes(pickle.dumps([solid]))
    loaded = _pk(tmp_path, "solids")
    assert loaded[0].frames[0][1].shape == (20, 30)
    assert path.stat().st_size < 2000


def test_dead_noise_and_late_births_do_not_retain_histories(monkeypatch):
    import weakref
    import keepframe.analyze.solids as module

    frames = np.zeros((40, 80, 100, 3), np.uint8)
    refs = []
    def candidate():
        solid = Solid()
        refs.append(weakref.ref(solid))
        return solid
    monkeypatch.setattr(module, "Solid", candidate)
    def foreground():
        for f in range(len(frames)):
            assert sum(ref() is not None for ref in refs) <= 1
            mask = np.zeros((80, 100), bool)
            x = 10 if f % 2 else 70
            mask[20:25, x:x + 5] = True
            yield mask
    assert module.find_solids(frames, foreground(), [], np.zeros(frames.shape[:3], bool)) == []
    assert len(refs) == 29  # No new history in the final eleven frames (life floor=12).


# Frozen pre-fix tracker: check cropped tracking against the original semantics.
def _legacy_find_solids(frames, fg, obj_tracks, text_masks, min_life=12, min_members=3, change_thr=0.15) -> list[Solid]:
    """Track 5px-dilated non-text foreground components and qualify interior change."""
    kernel = np.ones((11, 11), np.uint8)  # radius 5px
    candidates = []
    previous = []
    # ponytail: adjacent-frame one-to-one IoU; bridge occlusion gaps with a predictive component tracker.
    for f in range(len(frames)):
        foreground = fg[f].astype(bool) & ~text_masks[f].astype(bool)
        count, labels = cv2.connectedComponents(cv2.dilate(foreground.astype(np.uint8), kernel))
        components = [labels == i for i in range(1, count)]
        pairs = []
        for i, (_, a) in enumerate(previous):
            for j, b in enumerate(components):
                overlap = np.count_nonzero(a & b)
                iou = overlap / max(1, np.count_nonzero(a | b))
                if iou >= 0.5:
                    pairs.append((iou, i, j))
        matches, used = {}, set()
        for _, i, j in sorted(pairs, reverse=True):
            if i not in used and j not in matches:
                matches[j] = previous[i][0]
                used.add(i)
        current = []
        for j, component in enumerate(components):
            solid = matches.get(j)
            if solid is None:
                solid = Solid()
                candidates.append(solid)
            mask = component & foreground
            ys, xs = np.nonzero(mask)
            box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
            solid.frames[f] = (box, mask)
            current.append((solid, component))
        previous = current
    result = []
    for solid in candidates:
        life = len(solid.frames)
        if life < max(min_life, 0.30 * len(frames)):
            continue
        membership_masks = {f: cv2.dilate(mask.astype(np.uint8), kernel).astype(bool)
                            for f, (_, mask) in solid.frames.items()}
        members = []
        for track in obj_tracks:
            inside = 0
            for f, region in track.regions.items():
                if f in solid.frames:
                    mask = membership_masks[f]
                    x, y = (int(round(v)) for v in region.centroid)
                    inside += 0 <= y < mask.shape[0] and 0 <= x < mask.shape[1] and bool(mask[y, x])
            if inside >= len(track.regions) / 2:
                members.append(track)
        fragmented = len(members) >= min_members and np.median([len(t.regions) for t in members]) < life * 0.50
        stable, change = _change(frames, solid)
        if fragmented or (stable and change >= change_thr):
            solid.members = [t.id for t in members]
            result.append(solid)
    return result



@pytest.fixture(autouse=True)
def compare_legacy_tracking(request, monkeypatch):
    if request.node.originalname in {"test_thirty_moving_blobs_at_720p_finish_under_ten_seconds",
                                     "test_two_thousand_tiny_components_are_skipped_before_tracking",
                                     "test_changing_blob_uses_median_bbox_area_limit"}:
        return
    from keepframe.analyze.solids import solid_props
    optimized = find_solids

    def compared(*args, **kwargs):
        actual = optimized(*args, **kwargs)
        expected = _legacy_find_solids(*args, **kwargs)
        assert len(actual) == len(expected)
        for a, b in zip(actual, expected):
            assert a.members == b.members and a.frames.keys() == b.frames.keys()
            for f, (box, mask) in a.frames.items():
                assert box == b.frames[f][0]
                x0, y0, x1, y1 = box
                reference = b.frames[f][1][y0:y1, x0:x1]
                if mask.shape == args[0].shape[1:3]:
                    mask = mask[y0:y1, x0:x1]
                assert np.array_equal(mask, reference)
            # Full-frame and cropped flow masks must give the same crop/pose tracks.
            ap, bp = solid_props(a, args[0]), solid_props(b, args[0])
            np.testing.assert_array_equal(ap["canon"], bp["canon"])
            np.testing.assert_allclose(ap["raw"], bp["raw"], atol=1e-8)
        return actual

    monkeypatch.setattr(__import__(__name__, fromlist=["find_solids"]), "find_solids", compared)
