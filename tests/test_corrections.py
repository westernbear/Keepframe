import json, numpy as np, cv2
from refstudio.ir.synth import make_synthetic_scene
from refstudio.ir.store import current_scene, scene_dir, load_project
from refstudio.ir.schema import FontGuess
from refstudio.analyze.video import render_scene_video
from refstudio.analyze.pipeline import analyze, AnalyzeOptions
from refstudio.review.corrections import edit_text, reassign_id, add_bbox_prompt

def project(tmp, seed, frames=24):
    gold = make_synthetic_scene(tmp / "gold", seed=seed, frames=frames, with_text=False, overlap=False)
    vid = render_scene_video(gold, tmp / "gold", tmp / "gold.mp4")
    root = tmp / "proj"
    analyze(vid, 0, frames - 1, root, AnalyzeOptions(ocr=False, refine=False))
    return gold, root

def test_edit_text_creates_manual_version(tmp_scene_dir):
    _, root = project(tmp_scene_dir, 71)
    s, _ = current_scene(root, "s1")
    eid = s.elements[0].id
    v = edit_text(root, "s1", eid, text="Hello", font=FontGuess(size_px=48))
    assert v.id == "v2" and v.auto is False
    s2, _ = current_scene(root, "s1")
    assert s2.element(eid).canonical.text == "Hello" and s2.element(eid).provenance == "manual"
    assert load_project(root).versions[0].scene_file.endswith("scene.v1.json")

def test_reassign_whole_range_merges_elements(tmp_scene_dir):
    _, root = project(tmp_scene_dir, 72)
    s, _ = current_scene(root, "s1")
    a, b = s.elements[0].id, s.elements[1].id
    v = reassign_id(root, "s1", (0, s.frames - 1), from_id=b, to_id=a)
    s2, _ = current_scene(root, "s1")
    assert v.id == "v2" and len(s2.elements) == len(s.elements) - 1
    ov = json.loads((scene_dir(root, "s1") / "stages" / "overrides.json").read_text())
    assert ov["merge"]

def test_bbox_prompt_appends_override_and_reruns(tmp_scene_dir):
    _, root = project(tmp_scene_dir, 73)
    s, _ = current_scene(root, "s1")
    from refstudio.ir.tracks import element_bbox
    x0, y0, x1, y1 = [int(v) for v in element_bbox(s.elements[0], 0)]
    v = add_bbox_prompt(root, "s1", 0, (x0 - 2, y0 - 2, x1 + 2, y1 + 2), s.elements[0].id)
    assert v.id == "v2"
    ov = json.loads((scene_dir(root, "s1") / "stages" / "overrides.json").read_text())
    assert ov["regions"] and (scene_dir(root, "s1") / ov["regions"][0]["mask"]).exists()
    s2, _ = current_scene(root, "s1")
    assert len(s2.elements) == len(s.elements)
