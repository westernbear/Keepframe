import json, subprocess, sys
import math
from types import SimpleNamespace
import pytest
import numpy as np
from keepframe.ir.synth import make_synthetic_scene
from keepframe.ir.store import current_scene, scene_dir
from keepframe.analyze.video import render_scene_video, read_frames
from keepframe.analyze.pipeline import analyze, AnalyzeOptions
from keepframe.analyze.golden import compare
from keepframe.gates import m2_gate

def test_compare_on_analyzed_synthetic(tmp_scene_dir):
    gold = make_synthetic_scene(tmp_scene_dir / "gold", seed=81, with_text=False, overlap=False)
    vid = render_scene_video(gold, tmp_scene_dir / "gold", tmp_scene_dir / "gold.mp4")
    root = tmp_scene_dir / "proj"
    analyze(vid, 0, gold.frames - 1, root, AnalyzeOptions(ocr=False, refine=False))
    scene, _ = current_scene(root, "s1")
    frames, _ = read_frames(vid)
    m = compare(gold, tmp_scene_dir / "gold", scene, scene_dir(root, "s1"), frames)
    assert m["matched"] == len(gold.elements) and m["tracking_errors"] == 0
    assert m["pos_err_px"] < 2.5 and m["frame_l1"] < 0.03 and m["temporal"] > 0.8

def test_m2_gate_small(tmp_scene_dir):
    res = m2_gate(tmp_scene_dir, n=3, refine=False)
    expected = (res["frame_l1_ok"] >= math.ceil(0.8 * res["n"])
                and res["tracking_ok"] and res["temporal_mean"] >= 0.7)
    assert res["passed"] == expected

def test_compare_scale_error_includes_both_axes(tmp_path, monkeypatch):
    from keepframe.analyze import golden

    class Element:
        def __init__(self, texture):
            self.id = texture
            self.visible = (0, 1)
            self.canonical = SimpleNamespace(texture="texture.png")

    class Scene:
        frames = 2
        elements = [Element("e")]

        @staticmethod
        def element(element_id):
            return Scene.elements[0]

    golden_scene = Scene()
    analyzed_scene = Scene()
    monkeypatch.setattr(golden, "animation_matrix", lambda _scene: {
        "e": np.array([[0, 0, 1., 1., 0, 1], [1, 1, 1., 1., 0, 1]])
    } if _scene is golden_scene else {
        "e": np.array([[0, 0, 1., 1.2, 0, 1], [1, 1, 1., 1.2, 0, 1]])
    })
    monkeypatch.setattr(golden, "load_texture", lambda _path: np.zeros((1, 1, 4), dtype=np.float32))
    monkeypatch.setattr(golden, "composite_scene", lambda *_args: np.zeros((1, 1, 3), dtype=np.float32))
    monkeypatch.setattr(golden, "temporal_similarity", lambda *_args: 1.0)
    monkeypatch.setattr(golden, "centroid_tracks", lambda _scene: {"e": np.array([[0., 0.], [1., 1.]])})

    result = golden.compare(golden_scene, tmp_path, analyzed_scene, tmp_path, np.zeros((2, 1, 1, 3)))

    assert result["scale_err"] == pytest.approx(0.1)

def test_real_gate_returns_empty_rows_for_missing_clip_directory(tmp_path):
    from keepframe.gates import m2_gate_real

    result = m2_gate_real(tmp_path / "clips", tmp_path / "out")

    assert result == {"clips": 0, "rows": []}


def test_real_gate_reports_null_for_unmatchable_annotations(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from keepframe import gates
    from keepframe.analyze import video
    from keepframe.ir import store
    from keepframe.verify.matrix import animation_matrix

    clips = tmp_path / "clips"
    clips.mkdir()
    clip = clips / "empty.mp4"
    clip.touch()
    clip.with_suffix(".gt.json").write_text(json.dumps({"elements": [{"frames": {}}]}))
    scene = SimpleNamespace(elements=[])
    monkeypatch.setattr(video, "read_frames", lambda _clip: (np.zeros((1, 1, 1, 3)), 30))
    monkeypatch.setattr(gates, "analyze", lambda *_args, **_kwargs: None, raising=False)
    monkeypatch.setattr("keepframe.analyze.pipeline.analyze", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(store, "current_scene", lambda *_args: (scene, None))
    monkeypatch.setattr(store, "scene_dir", lambda *_args: tmp_path / "scene")
    (tmp_path / "scene").mkdir()
    (tmp_path / "scene" / "report.json").write_text(json.dumps({"reconstruction": {"mean_l1": 0.0}, "messages": []}))
    monkeypatch.setattr("keepframe.verify.matrix.animation_matrix", lambda _scene: {})

    result = gates.m2_gate_real(clips, tmp_path / "out")

    assert result["clips"] == 1
    assert result["rows"][0]["pos_err_px"] is None


def test_real_gate_reports_malformed_ground_truth(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from keepframe import gates
    from keepframe.analyze import video
    from keepframe.ir import store

    clips = tmp_path / "clips"
    clips.mkdir()
    clip = clips / "bad.mp4"
    clip.touch()
    clip.with_suffix(".gt.json").write_text("not json")
    scene = SimpleNamespace(elements=[])
    monkeypatch.setattr(video, "read_frames", lambda _clip: (np.zeros((1, 1, 1, 3)), 30))
    monkeypatch.setattr("keepframe.analyze.pipeline.analyze", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(store, "current_scene", lambda *_args: (scene, None))
    monkeypatch.setattr(store, "scene_dir", lambda *_args: tmp_path / "scene")
    (tmp_path / "scene").mkdir()
    (tmp_path / "scene" / "report.json").write_text(json.dumps({"reconstruction": {"mean_l1": 0.0}, "messages": []}))

    result = gates.m2_gate_real(clips, tmp_path / "out")

    assert result["clips"] == 1
    assert result["rows"][0]["pos_err_px"] is None
    assert "ground truth" in result["rows"][0]["messages"][-1].lower()

def test_real_gate_keeps_processing_after_malformed_annotation_entry(tmp_path, monkeypatch):
    from keepframe import gates
    from keepframe.analyze import video
    from keepframe.ir import store

    clips = tmp_path / "clips"
    clips.mkdir()
    bad = clips / "a-bad.mp4"
    bad.touch()
    bad.with_suffix(".gt.json").write_text(json.dumps({"elements": [{"frames": None}]}))
    good = clips / "b-good.mp4"
    good.touch()
    good.with_suffix(".gt.json").write_text(json.dumps({"elements": [{"frames": {}}]}))
    scene = SimpleNamespace(elements=[])
    monkeypatch.setattr(video, "read_frames", lambda _clip: (np.zeros((1, 1, 1, 3)), 30))
    monkeypatch.setattr("keepframe.analyze.pipeline.analyze", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(store, "current_scene", lambda *_args: (scene, None))
    monkeypatch.setattr(store, "scene_dir", lambda *_args: tmp_path / "scene")
    (tmp_path / "scene").mkdir()
    (tmp_path / "scene" / "report.json").write_text(json.dumps({"reconstruction": {"mean_l1": 0.0}, "messages": []}))

    result = gates.m2_gate_real(clips, tmp_path / "out")

    assert [row["clip"] for row in result["rows"]] == ["a-bad.mp4", "b-good.mp4"]
    assert result["rows"][0]["pos_err_px"] is None
    assert "ground truth" in result["rows"][0]["messages"][-1].lower()
    assert result["rows"][1]["pos_err_px"] is None

@pytest.mark.parametrize("elements", [None, {"bad": {}}, 7])
def test_real_gate_keeps_processing_after_malformed_elements_container(tmp_path, monkeypatch, elements):
    from keepframe import gates
    from keepframe.analyze import video
    from keepframe.ir import store

    clips = tmp_path / "clips"
    clips.mkdir()
    bad = clips / "a-bad.mp4"
    bad.touch()
    bad.with_suffix(".gt.json").write_text(json.dumps({"elements": elements}))
    good = clips / "b-good.mp4"
    good.touch()
    good.with_suffix(".gt.json").write_text(json.dumps({"elements": []}))
    scene = SimpleNamespace(elements=[])
    monkeypatch.setattr(video, "read_frames", lambda _clip: (np.zeros((1, 1, 1, 3)), 30))
    monkeypatch.setattr("keepframe.analyze.pipeline.analyze", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(store, "current_scene", lambda *_args: (scene, None))
    monkeypatch.setattr(store, "scene_dir", lambda *_args: tmp_path / "scene")
    (tmp_path / "scene").mkdir()
    (tmp_path / "scene" / "report.json").write_text(json.dumps({"reconstruction": {"mean_l1": 0.0}, "messages": []}))

    result = gates.m2_gate_real(clips, tmp_path / "out")

    assert [row["clip"] for row in result["rows"]] == ["a-bad.mp4", "b-good.mp4"]
    assert result["rows"][0]["pos_err_px"] is None
    assert "ground truth" in result["rows"][0]["messages"][-1].lower()
    assert result["rows"][1]["pos_err_px"] is None
@pytest.mark.parametrize("l1,tracking,temporal,passed", [
    ([0.01, 0.01, 0.03], 0, 1.0, False),
    ([0.01, 0.01, 0.02], 5, 0.7, True),
    ([0.01, 0.01, 0.02], 6, 0.7, False),
    ([0.01, 0.01, 0.02], 5, 0.69, False),
])
def test_m2_gate_acceptance_boundaries(tmp_path, monkeypatch, l1, tracking, temporal, passed):
    from keepframe import gates
    from keepframe.analyze import golden, pipeline, video
    from keepframe.ir import store

    scene = SimpleNamespace(frames=1)
    monkeypatch.setattr(gates, "make_synthetic_scene", lambda *a, **kw: scene)
    monkeypatch.setattr(gates, "save_scene", lambda *a: None)
    monkeypatch.setattr(video, "render_scene_video", lambda scene, root, out: out)
    monkeypatch.setattr(video, "read_frames", lambda *a: (None, 30))
    monkeypatch.setattr(pipeline, "analyze", lambda *a: None)
    monkeypatch.setattr(store, "current_scene", lambda *a: (scene, None))
    errors = iter(l1)
    monkeypatch.setattr(golden, "compare", lambda *a: {
        "frame_l1": next(errors), "tracking_errors": tracking, "temporal": temporal,
    })

    assert m2_gate(tmp_path, n=3, refine=False)["passed"] is passed

def test_cli_analyze_and_correct(tmp_scene_dir):
    gold = make_synthetic_scene(tmp_scene_dir / "gold", seed=82, frames=24, with_text=False, overlap=False)
    vid = render_scene_video(gold, tmp_scene_dir / "gold", tmp_scene_dir / "gold.mp4")
    root = tmp_scene_dir / "proj"
    r = subprocess.run([sys.executable, "-m", "keepframe.cli", "analyze", "--video", str(vid), "--start", "0", "--end", "23",
                        "--out", str(root), "--no-ocr", "--no-refine"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    s, _ = current_scene(root, "s1")
    r = subprocess.run([sys.executable, "-m", "keepframe.cli", "correct", "--root", str(root), "--scene", "s1", "--op", "text",
                        "--args", json.dumps({"element_id": s.elements[0].id, "text": "Hi"})], capture_output=True, text=True)
    assert r.returncode == 0 and '"id": "v2"' in r.stdout
