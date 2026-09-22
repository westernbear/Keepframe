import json, numpy as np, pytest
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

def test_rerun_stage_boundaries_refresh_only_promised_inputs(tmp_scene_dir):
    gold = make_synthetic_scene(tmp_scene_dir / "gold", seed=63, frames=12, with_text=False, overlap=False)
    vid = render_scene_video(gold, tmp_scene_dir / "gold", tmp_scene_dir / "gold.mp4")
    root = tmp_scene_dir / "proj"
    analyze(vid, 2, 9, root, AnalyzeOptions(ocr=False, refine=False))
    project = json.loads((root / "project.json").read_text())
    assert project["source"]["range"] == [2, 9]
    stages = root / "scenes" / "s1" / "stages"
    assert len(np.load(stages / "frames.npy")) == 8
    assert json.loads((stages / "overrides.json").read_text()) == {"regions": [], "ids": {}, "merge": []}

    frames_path = stages / "frames.npy"
    original_frames = frames_path.read_bytes()
    frames_path.write_bytes(b"stale cache")
    rerun(root, "s1", "frames", note="refresh source")
    assert frames_path.read_bytes() == original_frames

    original_frames = frames_path.read_bytes()
    (stages / "background.json").write_text("stale cache")
    rerun(root, "s1", "background", note="refresh background", options=AnalyzeOptions(bg_override="#112233", ocr=False, refine=False))
    assert frames_path.read_bytes() == original_frames
    assert json.loads((stages / "background.json").read_text())["rgb"] == [17, 34, 51]
    scene, version = current_scene(root, "s1")
    assert scene.background.value == "#112233"
    assert version.id == "v3" and version.parent == "v2" and version.auto is False

    original_background = (stages / "background.json").read_bytes()
    v4 = rerun(root, "s1", "keyframes", note="reuse upstream caches")
    assert frames_path.read_bytes() == original_frames
    assert (stages / "background.json").read_bytes() == original_background
    assert v4.id == "v4" and v4.parent == "v3" and v4.auto is False
    assert current_scene(root, "s1")[1].id == "v4"

    project["source"]["file"] = str(tmp_scene_dir / "missing.mp4")
    (root / "project.json").write_text(json.dumps(project))
    with pytest.raises(FileNotFoundError):
        rerun(root, "s1", "frames", note="missing source")

def test_rerun_from_keyframes_appends_version(tmp_scene_dir):
    gold = make_synthetic_scene(tmp_scene_dir / "gold", seed=62, frames=24, with_text=False, overlap=False)
    vid = render_scene_video(gold, tmp_scene_dir / "gold", tmp_scene_dir / "gold.mp4")
    root = tmp_scene_dir / "proj"
    analyze(vid, 0, 23, root, AnalyzeOptions(ocr=False, refine=False))
    v2 = rerun(root, "s1", "keyframes", note="rerun test")
    assert v2.id == "v2" and v2.auto is False
    s1, _ = current_scene(root, "s1")
    assert len(s1.elements) == len(gold.elements)
