import json, pickle
import cv2, numpy as np, pytest
from keepframe.analyze.pipeline import AnalyzeOptions, _stage_text, analyze, rerun
from keepframe.analyze.text import TextBox, TextTrack, drop_junk_text, keep_text_track
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


def test_drop_junk_text_removes_their_boxes_from_frames():
    good, junk = _track("Sale", [0, 1, 2], tid=1), _track("0", [1], tid=2)
    frames = [[good.boxes[0]], [good.boxes[1], junk.boxes[1]], [good.boxes[2]]]
    kept_frames, kept, dropped = drop_junk_text(frames, [good, junk])
    assert [t.id for t in kept] == [1] and dropped == 1
    assert [len(f) for f in kept_frames] == [1, 1, 1] and junk.boxes[1] not in kept_frames[1]
    assert kept[0] is good and kept_frames[1][0] is good.boxes[1]
    assert len(frames[1]) == 2


def test_drop_junk_text_when_no_tracks_survive():
    junk = _track("O", [0, 1, 2])
    assert drop_junk_text([[junk.boxes[f]] for f in range(3)], [junk]) == ([[], [], []], [], 1)
    assert drop_junk_text([[], []], []) == ([[], []], [], 0)


def test_text_stage_applies_copy_before_hygiene(tmp_path):
    frames = np.full((3, 40, 50, 3), 255, np.uint8)
    boxes, tracks, message = _stage_text(frames, (255, 255, 255), AnalyzeOptions(copy=["Oo"]),
                                        lambda frame: [("O", (10, 10, 40, 30), 0.4)], tmp_path)
    assert len(tracks) == 1 and tracks[0].text == "Oo"
    assert [len(frame) for frame in boxes] == [1, 1, 1] and message is None


@pytest.fixture
def noisy_ocr_clip(tmp_path, monkeypatch):
    frames = np.full((6, 96, 144, 3), 255, np.uint8)
    for frame in frames:
        cv2.putText(frame, "Sale", (8, 29), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 2)
    cv2.circle(frames[2], (112, 66), 12, (0, 80, 220), -1)

    class FakeOcr:
        def __init__(self):
            self.frame = 0

        def __call__(self, frame):
            boxes = [("Sale", (6, 10, 66, 33), 0.95)]
            if self.frame == 2:
                boxes.append(("O", (98, 52, 126, 80), 0.4))
            self.frame += 1
            return boxes

    monkeypatch.setattr("keepframe.analyze.pipeline.read_frames", lambda *args: (frames, 30.0))
    monkeypatch.setattr("keepframe.analyze.text.RapidOcr", FakeOcr)
    root = tmp_path / "project"
    options = AnalyzeOptions(bg_override="#ffffff", refine=False, use_ecc=False)
    analyze(tmp_path / "clip.mp4", 0, 5, root, options, ocr=FakeOcr())
    return root


def _assert_clean_scene(root):
    scene, version = current_scene(root, "s1")
    assert [e.canonical.text for e in scene.elements if e.kind == "text"] == ["Sale"]
    sprites = [e for e in scene.elements if e.kind == "sprite"]
    assert len(sprites) == 1 and sprites[0].visible == (2, 2)
    assert (sprites[0].canonical.width, sprites[0].canonical.height) == (25, 25)
    sd = scene_dir(root, "s1")
    text = pickle.loads((sd / "stages/text.pkl").read_bytes())
    assert [track.text for track in text["tracks"]] == ["Sale"]
    assert [len(frame) for frame in text["boxes"]] == [1] * 6
    assert all(box.text == "Sale" for frame in text["boxes"] for box in frame)
    overlay = frame_overlay(root, scene, version, 2)
    assert [entry["text"] for obj in overlay["objects"] for entry in obj["ocr"]] == ["Sale"]
    assert any(obj["kind"] == "sprite" for obj in overlay["objects"])
    return json.loads((sd / "report.json").read_text())


def test_analyze_drops_noise_and_recovers_shape(noisy_ocr_clip):
    report = _assert_clean_scene(noisy_ocr_clip)
    assert report["messages"] == ["text tracks dropped as OCR noise: 1"]


@pytest.mark.parametrize("stage", ["text", "regions", "keyframes"])
def test_rerun_uses_filtered_text_and_reports_new_drops(noisy_ocr_clip, stage):
    text_path = scene_dir(noisy_ocr_clip, "s1") / "stages/text.pkl"
    cached_text = text_path.read_bytes()
    version = rerun(noisy_ocr_clip, "s1", stage, note="text hygiene regression")
    assert version.id == "v2"
    report = _assert_clean_scene(noisy_ocr_clip)
    assert report["messages"] == (["text tracks dropped as OCR noise: 1"] if stage == "text" else [])
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

    def fail():
        raise RuntimeError("OCR unavailable")

    monkeypatch.setattr("keepframe.analyze.text.RapidOcr", fail)
    if stage == "analyze":
        analyze(tmp_path / "clip.mp4", 0, 2, root, options)
    else:
        rerun(root, "s1", "text", note="OCR failure", options=options)
    report = json.loads((scene_dir(root, "s1") / "report.json").read_text())
    assert report["messages"] == ["text stage skipped: OCR unavailable"]
