import json, numpy as np
from keepframe.ir.synth import make_synthetic_scene
from keepframe.ir.store import current_scene, scene_dir
from keepframe.analyze.video import render_scene_video
from keepframe.analyze.pipeline import analyze, rerun, AnalyzeOptions

def test_analyze_synthetic_video_end_to_end(tmp_scene_dir):
    gold = make_synthetic_scene(tmp_scene_dir / "gold", seed=61, with_text=False, overlap=False)
    vid = render_scene_video(gold, tmp_scene_dir / "gold", tmp_scene_dir / "gold.mp4")
    root = tmp_scene_dir / "proj"
    project = analyze(vid, 0, gold.frames - 1, root, AnalyzeOptions(ocr=False, refine=False))
    scene, v = current_scene(root, "s1")
    assert v.id == "v1" and scene.frames == gold.frames and scene.size == gold.size
    assert len(scene.elements) == len(gold.elements)
    assert all(e.raw and (scene_dir(root, "s1") / e.raw).exists() for e in scene.elements)
    assert all(e.canonical.texture and (scene_dir(root, "s1") / e.canonical.texture).exists() for e in scene.elements)
    assert scene.constraints and all(c.keep is False for c in scene.constraints)
    rep = json.loads((scene_dir(root, "s1") / "report.json").read_text())
    assert rep["reconstruction"]["mean_l1"] < 0.03
    assert set(rep["confidence"]) == {e.id for e in scene.elements}
    assert (scene_dir(root, "s1") / "stages" / "ids.json").exists()

def test_rerun_from_keyframes_appends_version(tmp_scene_dir):
    gold = make_synthetic_scene(tmp_scene_dir / "gold", seed=62, frames=24, with_text=False, overlap=False)
    vid = render_scene_video(gold, tmp_scene_dir / "gold", tmp_scene_dir / "gold.mp4")
    root = tmp_scene_dir / "proj"
    analyze(vid, 0, 23, root, AnalyzeOptions(ocr=False, refine=False))
    v2 = rerun(root, "s1", "keyframes", note="rerun test")
    assert v2.id == "v2" and v2.auto is False
    s1, _ = current_scene(root, "s1")
    assert len(s1.elements) == len(gold.elements)
