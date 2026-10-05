import json

import numpy as np
import pytest

from keepframe.analyze.pipeline import AnalyzeOptions, analyze_scene_frames
from keepframe.analyze.solids import estimate_spin, find_solids
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
    assert np.max(np.abs(rx)) < 5


def test_rigid_gradient_card_is_not_solid():
    frames = np.zeros((30, 140, 200, 3), np.uint8)
    fg = np.zeros(frames.shape[:3], bool)
    card = np.broadcast_to(np.linspace(40, 240, 72, dtype=np.uint8)[None, :, None], (48, 72, 3))
    for f in range(len(frames)):
        frames[f, 40:88, 20 + f:92 + f] = card
        fg[f, 40:88, 20 + f:92 + f] = True
    assert find_solids(frames, fg, [], np.zeros_like(fg)) == []


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
