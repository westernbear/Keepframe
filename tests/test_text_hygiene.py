import json, pickle
import cv2, numpy as np, pytest
from keepframe.analyze import text as text_module
from keepframe.analyze.composite import composite_scene
from keepframe.analyze.pipeline import AnalyzeOptions, _stage_text, analyze, rerun
from keepframe.analyze.text import TextBox, TextTrack, keep_text_track
from keepframe.ir.store import current_scene, scene_dir
from keepframe.review.overlay import frame_overlay


def _track(text, frames, conf=0.5, tid=1):
    return TextTrack(id=tid, text=text, boxes={f: TextBox(f, text, (10, 10, 40, 30), conf) for f in frames})


def test_keep_rules():
    assert not keep_text_track(_track("Sale", [0]))
    assert not keep_text_track(_track("O", [0, 1, 2], conf=0.5))
    assert keep_text_track(_track("O", [0, 1, 2], conf=0.95))
    assert not keep_text_track(_track("·...", [0, 1, 2, 3], conf=0.9))
    assert keep_text_track(_track("Sale", [0, 1, 2]))
    assert keep_text_track(_track("가나", [0, 1, 2]))


def test_keep_rules_boundaries_and_mean_confidence():
    assert not keep_text_track(_track("Sale", []))
    assert not keep_text_track(_track("Sale", [0, 1], conf=1.0))
    assert keep_text_track(_track("Sale", [0, 2, 4], conf=0.1))
    assert keep_text_track(_track("0", [0, 1, 2], conf=0.9))
    assert not keep_text_track(_track("0", [0, 1, 2], conf=0.899))
    assert not keep_text_track(_track("·...", [0, 1, 2], conf=1.0))
    assert not keep_text_track(_track(" ", [0, 1, 2], conf=1.0))
    track = _track("O", [0, 1, 2], conf=0.95)
    track.boxes[0].conf = 0.85
    assert keep_text_track(track)
    track.boxes[0].conf = 0.7
    assert not keep_text_track(track)


def test_split_junk_text_preserves_tracks_and_boxes():
    good, junk = _track("Sale", [0, 1, 2], tid=1), _track("0", [1], tid=2)
    tracks = [good, junk]
    kept, shapes, count = text_module.split_junk_text(tracks)
    assert [t.id for t in kept] == [1] and [t.id for t in shapes] == [2] and count == 1
    assert kept[0] is good and shapes[0] is junk and tracks == [good, junk]
    assert shapes[0].boxes[1] is junk.boxes[1]


def test_split_junk_text_when_no_text_survives():
    junk = _track("O", [0, 1, 2])
    assert text_module.split_junk_text([junk]) == ([], [junk], 1)
    assert text_module.split_junk_text([]) == ([], [], 0)


def test_text_stage_applies_copy_before_hygiene(tmp_path):
    frames = np.full((3, 40, 50, 3), 255, np.uint8)
    boxes, tracks, shapes, message = _stage_text(frames, (255, 255, 255), AnalyzeOptions(copy=["Oo"]),
                                                lambda frame: [("O", (10, 10, 40, 30), 0.4)], tmp_path)
    assert len(tracks) == 1 and tracks[0].text == "Oo"
    assert [len(frame) for frame in boxes] == [1, 1, 1] and shapes == [] and message is None


def test_shape_props_keep_geometry_without_font_inference(monkeypatch):
    frames = np.full((3, 40, 50, 3), 255, np.uint8)
    frames[:, 15:25, 20:30] = (0, 80, 220)
    track = _track("O", [0, 1, 2])
    calls = []
    monkeypatch.setattr(text_module, "font_candidates", lambda *args: calls.append(args) or ["DejaVu Sans"])
    text_raw, text_canon, text_cf, font, _ = text_module.text_props(track, frames, (255, 255, 255), 3, 0)
    raw, canon, cf, shape_font, color = text_module.text_props(track, frames, (255, 255, 255), 3, 0, infer_font=False)
    np.testing.assert_array_equal(raw, text_raw)
    np.testing.assert_array_equal(canon, text_canon)
    assert cf == text_cf and shape_font is None and color is None
    assert len(calls) == 1 and font.candidates == ["DejaVu Sans"]


@pytest.fixture
def noisy_ocr_clip(tmp_path, monkeypatch):
    frames = np.full((6, 96, 144, 3), 255, np.uint8)
    for frame in frames:
        cv2.putText(frame, "Sale", (8, 29), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 2)
    cv2.circle(frames[2], (112, 66), 12, (0, 80, 220), -1)

    class FakeOcr:
        def __init__(self, max_side=1280):
            self.frame = 0

        def __call__(self, frame):
            boxes = [("Sale", (6, 10, 66, 33), 0.95)]
            if self.frame == 2:
                boxes.append(("O", (98, 52, 126, 80), 0.4))
            self.frame += 1
            return boxes

    monkeypatch.setattr("keepframe.analyze.pipeline.read_frames", lambda *args: (frames, 30.0))
    monkeypatch.setattr("keepframe.analyze.text.RapidOcr", FakeOcr)

    def rank(stroke, text, size):
        assert text == "Sale", "shape tracks must skip font candidates"
        return ["DejaVu Sans", "Liberation Sans", "DejaVu Serif"]

    monkeypatch.setattr(text_module, "font_candidates", rank)
    root = tmp_path / "project"
    options = AnalyzeOptions(bg_override="#ffffff", refine=False, use_ecc=False)
    analyze(tmp_path / "clip.mp4", 0, 5, root, options, ocr=FakeOcr())
    return root


def _assert_clean_scene(root):
    scene, version = current_scene(root, "s1")
    assert [e.canonical.text for e in scene.elements if e.kind == "text"] == ["Sale"]
    assert next(e for e in scene.elements if e.kind == "text").canonical.font.candidates == ["DejaVu Sans", "Liberation Sans", "DejaVu Serif"]
    sprites = [e for e in scene.elements if e.kind == "sprite"]
    assert len(sprites) == 1 and sprites[0].visible == (2, 2)
    sprite = sprites[0]
    assert (sprite.canonical.width, sprite.canonical.height) == (28, 28)
    assert sprite.canonical.text is None and sprite.canonical.font is None and sprite.canonical.color is None
    sd = scene_dir(root, "s1")
    text = pickle.loads((sd / "stages/text.pkl").read_bytes())
    assert [track.text for track in text["tracks"]] == ["Sale"]
    assert [(track.id, track.text) for track in text["shape_tracks"]] == [(2, "O")]
    assert [len(frame) for frame in text["boxes"]] == [1, 1, 2, 1, 1, 1]
    assert text["shape_tracks"][0].boxes[2] is text["boxes"][2][1]
    ids = json.loads((sd / "stages/ids.json").read_text())
    assert ids["s2"] == sprite.id and set(ids) == {"t1", "s2"}
    props = pickle.loads((sd / "stages/props.pkl").read_bytes())
    assert props["s2"]["kind"] == "sprite" and not {"text", "font", "color"} & props["s2"].keys()
    assert all(not regions for regions in pickle.loads((sd / "stages/regions.pkl").read_bytes()))
    frames = np.load(sd / "stages/frames.npy")
    crop = frames[2, 52:80, 98:126]
    rgba = cv2.cvtColor(cv2.imread(str(sd / sprite.canonical.texture), cv2.IMREAD_UNCHANGED), cv2.COLOR_BGRA2RGBA)
    np.testing.assert_array_equal(rgba[..., :3], crop)
    np.testing.assert_array_equal(rgba[..., 3], np.any(crop != 255, axis=2).astype(np.uint8) * 255)
    np.testing.assert_allclose(composite_scene(scene, sd, 2)[52:80, 98:126] * 255, crop, atol=1e-4)
    overlay = frame_overlay(root, scene, version, 2)
    assert [entry["text"] for obj in overlay["objects"] for entry in obj["ocr"]] == ["Sale"]
    assert [obj["kind"] for obj in overlay["objects"]] == ["text"]
    return json.loads((sd / "report.json").read_text())


def test_analyze_reclassifies_noise_without_losing_pixels(noisy_ocr_clip, monkeypatch):
    report = _assert_clean_scene(noisy_ocr_clip)
    assert report["messages"] == ["text tracks reclassified as shapes: 1", "3D 후보 0개"]
    monkeypatch.setattr(text_module, "keep_text_track", lambda track: True)
    monkeypatch.setattr(text_module, "font_candidates", lambda *args: [])
    baseline = noisy_ocr_clip.parent / "hygiene-disabled"
    analyze(noisy_ocr_clip.parent / "clip.mp4", 0, 5, baseline,
            AnalyzeOptions(bg_override="#ffffff", refine=False, use_ecc=False), ocr=text_module.RapidOcr())
    baseline_report = json.loads((scene_dir(baseline, "s1") / "report.json").read_text())
    baseline_scene, _ = current_scene(baseline, "s1")
    assert [e.canonical.text for e in baseline_scene.elements] == ["Sale", "O"]
    assert report["reconstruction"]["mean_l1"] <= baseline_report["reconstruction"]["mean_l1"] + 1e-12


@pytest.mark.parametrize("stage", ["text", "regions", "tracking", "sprites", "keyframes"])
def test_rerun_reuses_shape_tracks_and_reports_new_reclassifications(noisy_ocr_clip, stage):
    sd = scene_dir(noisy_ocr_clip, "s1")
    text_path = sd / "stages/text.pkl"
    cached_text = text_path.read_bytes()
    cached_ids = (sd / "stages/ids.json").read_bytes()
    before, _ = current_scene(noisy_ocr_clip, "s1")
    version = rerun(noisy_ocr_clip, "s1", stage, note="text hygiene regression")
    assert version.id == "v2"
    report = _assert_clean_scene(noisy_ocr_clip)
    assert report["messages"] == (["text tracks reclassified as shapes: 1"] if stage == "text" else []) + ["3D 후보 0개"]
    after, _ = current_scene(noisy_ocr_clip, "s1")
    assert [(e.id, e.kind) for e in after.elements] == [(e.id, e.kind) for e in before.elements]
    assert (sd / "stages/ids.json").read_bytes() == cached_ids
    if stage != "text":
        assert text_path.read_bytes() == cached_text


@pytest.mark.parametrize("stage", ["analyze", "text"])
def test_ocr_failure_message_still_reaches_report(tmp_path, monkeypatch, stage):
    frames = np.full((3, 40, 50, 3), 255, np.uint8)
    monkeypatch.setattr("keepframe.analyze.pipeline.read_frames", lambda *args: (frames, 30.0))
    root = tmp_path / "project"
    options = AnalyzeOptions(bg_override="#ffffff", refine=False, use_ecc=False)
    if stage == "text":
        analyze(tmp_path / "clip.mp4", 0, 2, root,
                AnalyzeOptions(bg_override="#ffffff", ocr=False, refine=False, use_ecc=False))

    def fail(max_side=1280):
        raise RuntimeError("OCR unavailable")

    monkeypatch.setattr("keepframe.analyze.text.RapidOcr", fail)
    if stage == "analyze":
        analyze(tmp_path / "clip.mp4", 0, 2, root, options)
    else:
        rerun(root, "s1", "text", note="OCR failure", options=options)
    report = json.loads((scene_dir(root, "s1") / "report.json").read_text())
    assert report["messages"] == ["text stage skipped: OCR unavailable", "3D 후보 0개"]
