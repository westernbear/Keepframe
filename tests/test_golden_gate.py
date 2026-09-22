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
