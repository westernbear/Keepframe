import json, pickle, numpy as np, cv2, pytest
from keepframe.ir.synth import make_synthetic_scene
from keepframe.ir.store import current_scene, scene_dir, load_project
from keepframe.ir.schema import FontGuess
from keepframe.analyze.video import render_scene_video
from keepframe.analyze.pipeline import analyze, AnalyzeOptions
from keepframe.review.corrections import _object_num, edit_text, reassign_id, add_bbox_prompt, set_region_mask


@pytest.fixture
def ocr_shape_project(tmp_path, monkeypatch):
    frames = np.full((6, 96, 144, 3), 255, np.uint8)
    for frame in frames:
        cv2.putText(frame, "Sale", (8, 29), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 2)
        frame[52:80, 10:35] = (220, 0, 80)
        frame[52:80, 50:75] = (0, 180, 80)
    cv2.circle(frames[2], (112, 66), 12, (0, 80, 220), -1)

    class FakeOcr:
        def __call__(self, frame):
            boxes = [("Sale", (6, 10, 66, 33), 0.95)]
            if tuple(frame[66, 112]) == (0, 80, 220):
                boxes.append(("O", (98, 52, 126, 80), 0.4))
            return boxes

    monkeypatch.setattr("keepframe.analyze.pipeline.read_frames", lambda *args: (frames, 30.0))
    root = tmp_path / "ws" / "p1"
    analyze(tmp_path / "clip.mp4", 0, 5, root,
            AnalyzeOptions(bg_override="#ffffff", refine=False, use_ecc=False), ocr=FakeOcr())
    ids = json.loads((scene_dir(root, "s1") / "stages/ids.json").read_text())
    assert set(ids) == {"t1", "s2", "o1", "o2"}
    return root


@pytest.mark.parametrize("key", ["s2", "t2", "x2", ""])
def test_object_num_rejects_non_object_keys(key):
    with pytest.raises(ValueError):
        _object_num(key)


def test_object_num_accepts_object_key():
    assert _object_num("o2") == 2


@pytest.mark.parametrize("key,op", [("s2", "mask"), ("s2", "bbox"), ("s2", "reassign_from"),
                                   ("s2", "reassign_to"), ("t1", "mask"),
                                   ("t1", "reassign_from"), ("t1", "reassign_to")])
def test_track_corrections_reject_ocr_targets_without_mutation(ocr_shape_project, key, op):
    root = ocr_shape_project
    sd = scene_dir(root, "s1")
    stages = sd / "stages"
    ids = json.loads((stages / "ids.json").read_text())
    mask = root / "mask.png"
    cv2.imwrite(str(mask), np.full((96, 144), 255, np.uint8))
    before = {p.name: p.read_bytes() for p in stages.iterdir() if p.is_file()}
    project_before = (root / "project.json").read_bytes()
    scene, version = current_scene(root, "s1")
    with pytest.raises(ValueError, match="shape elements from OCR boxes" if key == "s2" else "text"):
        if op == "mask":
            set_region_mask(root, "s1", 2, mask, ids[key])
        elif op == "bbox":
            add_bbox_prompt(root, "s1", 2, (98, 52, 126, 80), ids[key])
        elif op == "reassign_from":
            reassign_id(root, "s1", (2, 3), ids[key], ids["o2"])
        else:
            reassign_id(root, "s1", (2, 3), ids["o2"], ids[key])
    assert {p.name: p.read_bytes() for p in stages.iterdir() if p.is_file()} == before
    assert (root / "project.json").read_bytes() == project_before
    assert current_scene(root, "s1") == (scene, version)
    assert list(sd.glob("scene.v*.json")) == [sd / "scene.v1.json"]


@pytest.mark.parametrize("op", ["mask", "bbox", "reassign"])
def test_object_track_corrections_still_rerun(ocr_shape_project, op):
    root = ocr_shape_project
    stages = scene_dir(root, "s1") / "stages"
    ids = json.loads((stages / "ids.json").read_text())
    if op == "mask":
        mask = root / "mask.png"
        pixels = np.zeros((96, 144), np.uint8)
        pixels[52:80, 50:75] = 255
        cv2.imwrite(str(mask), pixels)
        version = set_region_mask(root, "s1", 2, mask, ids["o2"])
    elif op == "bbox":
        version = add_bbox_prompt(root, "s1", 2, (50, 52, 75, 80), ids["o2"])
    else:
        version = reassign_id(root, "s1", (2, 3), ids["o1"], ids["o2"])
    assert version.id == "v2" and current_scene(root, "s1")[1] == version
    overrides = json.loads((stages / "overrides.json").read_text())
    assert [r["frame"] for r in overrides["regions"]] == ([2, 3] if op == "reassign" else [2])
    assert all(r["label"] == 1002 and (scene_dir(root, "s1") / r["mask"]).exists()
               for r in overrides["regions"])
    scene, _ = current_scene(root, "s1")
    assert scene.element(ids["s2"]).kind == "sprite"


def test_text_bbox_correction_still_updates_text_track(ocr_shape_project):
    root = ocr_shape_project
    stages = scene_dir(root, "s1") / "stages"
    ids = json.loads((stages / "ids.json").read_text())
    overrides_before = (stages / "overrides.json").read_bytes()
    version = add_bbox_prompt(root, "s1", 1, (5, 9, 67, 34), ids["t1"])
    assert version.id == "v2"
    track = pickle.loads((stages / "text.pkl").read_bytes())["tracks"][0]
    assert track.boxes[1].bbox == (5, 9, 67, 34) and track.boxes[1].source == "manual_box"
    assert (stages / "overrides.json").read_bytes() == overrides_before


def test_whole_range_reassign_allows_ocr_shape_merge(ocr_shape_project):
    root = ocr_shape_project
    stages = scene_dir(root, "s1") / "stages"
    ids = json.loads((stages / "ids.json").read_text())
    version = reassign_id(root, "s1", (0, 5), ids["s2"], ids["o2"])
    assert version.id == "v2"
    overrides = json.loads((stages / "overrides.json").read_text())
    assert overrides["merge"] == [["o2", "s2"]] and overrides["regions"] == []

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
    from keepframe.ir.tracks import element_bbox
    x0, y0, x1, y1 = [int(v) for v in element_bbox(s.elements[0], 0)]
    old_version = load_project(root).versions[-1]
    from keepframe.review.overlay import frame_overlay
    before = frame_overlay(root, s, old_version, 0)
    v = add_bbox_prompt(root, "s1", 0, (x0 - 2, y0 - 2, x1 + 2, y1 + 2), s.elements[0].id)
    assert v.id == "v2"
    assert v.analysis_file and old_version.analysis_file
    assert frame_overlay(root, s, old_version, 0) == before
    ov = json.loads((scene_dir(root, "s1") / "stages" / "overrides.json").read_text())
    assert ov["regions"] and (scene_dir(root, "s1") / ov["regions"][0]["mask"]).exists()
    s2, _ = current_scene(root, "s1")
    assert len(s2.elements) == len(s.elements)
