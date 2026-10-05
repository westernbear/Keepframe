import cv2
import numpy as np
import pytest

from keepframe.analyze.composite import composite_scene
from keepframe.analyze.pipeline import AnalyzeOptions, analyze, analyze_scene_frames, rerun
from keepframe.analyze import text as text_module
from keepframe.analyze.text import TextBox, TextTrack
from keepframe.ir.store import current_scene, scene_dir
from keepframe.ir.tracks import element_bbox, eval_props
from keepframe.review.corrections import reassign_id


def test_letter_by_letter_ocr_becomes_one_reveal_without_sprite_fragments(tmp_path):
    readings = [("Y", 20)] * 4 + [("You", 60)] * 3 + [("You ju", 120)] * 2 + [("You just", 200)] * 3
    frames = np.full((len(readings), 80, 260, 3), 255, np.uint8)
    title = np.full((30, 200, 3), 255, np.uint8)
    cv2.putText(title, "You just", (0, 24), cv2.FONT_HERSHEY_SIMPLEX, 1.3, (0, 0, 0), 2)
    title = np.where(title < 128, 0, 255).astype(np.uint8)  # Exact pixel comparisons avoid the stroke threshold's antialias cutoff.
    for f, (_, width) in enumerate(readings):
        frames[f, 20:50, 20:20 + width] = title[:, :width]
        if width < 200:
            # The next partly visible letter falls outside OCR's partial box.
            frames[f, 30:40, 24 + width:32 + width] = 0
    detections = iter(readings)

    def fake_ocr(frame):
        text, width = next(detections)
        return [(text, (20, 20, 20 + width, 50), 0.6)]

    scene = analyze_scene_frames(frames, 30.0, tmp_path, "s1",
                                 AnalyzeOptions(bg_override="#ffffff", refine=False, use_ecc=False), fake_ocr)
    texts = [element for element in scene.elements if element.kind == "text"]
    assert len(texts) == 1
    text = texts[0]
    assert text.canonical.text == "You just"
    assert text.tracks["reveal"].keys[0].v == pytest.approx(0.1, abs=0.01)
    assert text.tracks["reveal"].keys[-1].v == 1.0
    assert [eval_props(text, frame)["reveal"] for frame in (0, 4, 7, 9)] == pytest.approx([0.1, 0.3, 0.6, 1.0], abs=0.01)
    assert element_bbox(text, 0) == element_bbox(text, 9) == (20, 20, 220, 50)
    assert not [element for element in scene.elements if element.kind == "sprite"]
    for f, width in ((0, 20), (4, 60), (7, 120), (9, 200)):
        expected = np.full((80, 260, 3), 255, np.uint8)
        expected[20:50, 20:20 + width] = title[:, :width]
        np.testing.assert_allclose(composite_scene(scene, tmp_path / "scenes/s1", f) * 255, expected, atol=1e-4)
    with np.load(tmp_path / "scenes/s1" / text.raw) as measured:
        assert measured["raw"].shape == (12, 9)
        assert measured["cols"].tolist() == ["x", "y", "sx", "sy", "rot", "skx", "sky", "opacity", "reveal"]


def _track(tid, text, first, last, bbox):
    return TextTrack(id=tid, text=text,
                     boxes={f: TextBox(f, text, bbox, 0.6) for f in range(first, last + 1)})


def test_fragmented_prefix_tracks_fold_repeatedly_into_later_track_and_keep_boxes():
    tracks = [_track(1, "Y", 0, 1, (20, 20, 40, 50)),
              _track(2, "You", 4, 5, (20, 20, 80, 50)),
              _track(3, "You ju", 8, 9, (20, 20, 140, 50)),
              _track(4, "You just", 12, 13, (20, 20, 220, 50))]
    original_boxes = {f: box for track in tracks for f, box in track.boxes.items()}
    merged = text_module.merge_reveals(list(reversed(tracks)))
    assert merged == [tracks[-1]]
    assert merged[0].id == 4 and merged[0].text == "You just"
    assert merged[0].boxes == original_boxes
    assert all(merged[0].boxes[f] is box for f, box in original_boxes.items())
    assert text_module.reveal_exclusion_boxes(merged) == {f: [(20, 20, 220, 50)] for f in range(14)}


@pytest.mark.parametrize("texts", [("B", "bUi Ld"), ("bUi Ld", "B")])
def test_association_accepts_contained_boxes_and_normalized_prefixes(texts):
    boxes = [[TextBox(0, texts[0], (10, 10, 30, 30), 0.9)],
             [TextBox(1, texts[1], (10, 10, 230, 30), 0.9)]]
    tracks = text_module.track_text(boxes)
    assert len(tracks) == 1
    assert set(tracks[0].boxes) == {0, 1}


@pytest.mark.parametrize("dx,dy,gap,source,target,merges", [
    (10, 12, 3, "B", "Build", True),
    (0, 0, -3, "B", "Build", True),
    (11, 0, 3, "B", "Build", False),
    (0, 13, 3, "B", "Build", False),
    (0, 0, 4, "B", "Build", False),
    (0, 0, -4, "B", "Build", False),
    (0, 0, 3, "Build", "Build", False),
    (0, 0, 3, "Build", "B", False),
    (0, 0, 3, "You just", "ust", False),
    (0, 0, 3, "B U", "build", True),
    (0, 0, 3, "Straße", "STRASSE news", True),
])
def test_merge_requires_strict_prefix_left_edge_baseline_and_exact_gap(dx, dy, gap, source, target, merges):
    earlier = _track(1, source, 0, 5, (20, 20, 40, 60))
    later = _track(2, target, 5 + gap, 10 + gap, (20 + dx, 20 + dy, 220 + dx, 60 + dy))
    later_first_box = later.boxes[later.first]
    merged = text_module.merge_reveals([earlier, later])
    assert [track.id for track in merged] == ([2] if merges else [1, 2])
    if merges:
        assert merged[0].text == target
        assert merged[0].boxes[5 + gap] is later_first_box
    else:
        assert text_module.reveal_exclusion_boxes(merged) == {}


def test_merge_uses_requested_max_gap_and_widest_frame_text():
    earlier = _track(1, "B", 0, 2, (20, 20, 240, 60))
    later = _track(2, "Build", 7, 9, (20, 20, 220, 60))
    assert text_module.merge_reveals([earlier, later]) == [earlier, later]
    assert text_module.merge_reveals([earlier, later], max_gap=5) == [later]
    assert later.text == "B"  # Canonical copy follows the widest frame, rather than the latest frame.


def test_zoomed_suffix_and_shifted_reveals_stay_separate():
    title = _track(1, "You just", 0, 2, (20, 20, 220, 50))
    zoom = _track(2, "ust", 3, 5, (-120, -30, 260, 150))
    assert text_module.merge_reveals([title, zoom]) == [title, zoom]
    for bbox in ((100, 20, 220, 50), (60, 20, 180, 50)):
        prefix = _track(3, "You", 0, 2, bbox)
        full = _track(4, "You just", 3, 5, (20, 20, 220, 50))
        assert text_module.merge_reveals([prefix, full]) == [prefix, full]


def test_two_title_lines_remain_two_text_elements(tmp_path):
    readings = [("B", (20, 10, 40, 30))] * 3 + [("Build", (20, 55, 220, 75))] * 3
    frames = np.full((6, 100, 260, 3), 255, np.uint8)
    for f, (_, (x0, y0, x1, y1)) in enumerate(readings):
        frames[f, y0:y1, x0:x1] = 0
    detections = iter(readings)
    scene = analyze_scene_frames(frames, 30.0, tmp_path, "s1",
                                 AnalyzeOptions(bg_override="#ffffff", refine=False, use_ecc=False),
                                 lambda frame: [(*next(detections), 0.95)])
    assert [element.canonical.text for element in scene.elements if element.kind == "text"] == ["B", "Build"]
    assert all("reveal" not in element.tracks for element in scene.elements)


def test_reveal_measurements_use_widest_box_and_leave_missing_frames_nan():
    frames = np.full((7, 80, 260, 3), 255, np.uint8)
    frames[:, 20:50, 20:220] = 0
    earlier = _track(1, "Y", 1, 1, (20, -10, 170, 50))  # Greater area, but narrower than the full box.
    later = _track(2, "You just", 4, 5, (20, 20, 220, 50))
    merged, = text_module.merge_reveals([earlier, later])
    raw, canonical, cf, _, _ = text_module.text_props(merged, frames, (255, 255, 255), 7, 0, infer_font=False)
    assert raw.shape == (7, 9) and cf == 4 and canonical.shape == (30, 200, 4)
    assert raw[1, 8] == 0.75 and raw[4, 8] == raw[5, 8] == 1.0
    assert raw[1, 0] == 120 and raw[1, 2] == 1.0
    assert np.isnan(raw[[0, 2, 3, 6]]).all()


def test_full_width_text_and_ocr_shapes_have_no_reveal_or_lost_motion():
    frames = np.zeros((3, 80, 260, 3), np.uint8)
    track = _track(1, "Build", 0, 2, (20, 20, 220, 50))
    track.boxes[0].bbox = (20, 20, 120, 50)
    for infer_font in (True, False):
        raw, _, _, _, _ = text_module.text_props(track, frames, (255, 255, 255), 3, 0, infer_font=infer_font)
        np.testing.assert_array_equal(raw[:, 8], [1.0, 1.0, 1.0])
        assert raw[0, 0] == 70 and raw[0, 2] == 0.5


def test_reveal_fraction_is_clipped_to_zero_and_one():
    frames = np.zeros((3, 80, 260, 3), np.uint8)
    track = _track(1, "Build", 0, 2, (100, 20, 200, 50))
    track.reveal = True
    track.boxes[0].bbox = (20, 20, 40, 50)
    track.boxes[2].bbox = (210, 20, 240, 50)
    raw, _, _, _, _ = text_module.text_props(track, frames, (255, 255, 255), 3, 0, infer_font=False)
    np.testing.assert_array_equal(raw[:, 8], [0.0, 1.0, 1.0])


def test_region_rerun_keeps_full_reveal_exclusion_during_ocr_gaps(tmp_path, monkeypatch):
    frames = np.full((7, 80, 260, 3), 255, np.uint8)
    frames[:3, 20:50, 20:40] = 0
    frames[:3, 30:40, 50:60] = 0  # A partial next letter outside OCR's first box.
    frames[3:, 20:50, 20:220] = 0
    readings = iter([[("Y", (20, 20, 40, 50), 0.6)], [], []]
                    + [[("You just", (20, 20, 220, 50), 0.6)]] * 4)
    monkeypatch.setattr("keepframe.analyze.pipeline.read_frames", lambda *args: (frames, 30.0))
    options = AnalyzeOptions(bg_override="#ffffff", refine=False, use_ecc=False)
    analyze(tmp_path / "clip.mp4", 0, 6, tmp_path / "project", options, ocr=lambda frame: next(readings))
    root = tmp_path / "project"
    sd = scene_dir(root, "s1")
    cached_text = (sd / "stages/text.pkl").read_bytes()
    before, _ = current_scene(root, "s1")
    assert [(element.kind, element.canonical.text) for element in before.elements] == [("text", "You just")]
    assert before.elements[0].visible == (0, 6)
    rerun(root, "s1", "regions", note="reveal mask regression")
    after, _ = current_scene(root, "s1")
    assert [(element.id, element.kind) for element in after.elements] == [(element.id, element.kind) for element in before.elements]
    assert (sd / "stages/text.pkl").read_bytes() == cached_text
    assert eval_props(after.elements[0], 0)["reveal"] == pytest.approx(0.1)


@pytest.mark.parametrize("shape_is_target", [False, True])
def test_whole_range_sprite_corrections_accept_mixed_raw_widths(tmp_path, monkeypatch, shape_is_target):
    frames = np.full((6, 80, 180, 3), 255, np.uint8)
    frames[:3, 30:60, 10:40] = 0
    frames[3:, 30:60, 90:120] = 0
    readings = iter([[], [], []] + [[("O", (90, 30, 120, 60), 0.4)]] * 3)
    monkeypatch.setattr("keepframe.analyze.pipeline.read_frames", lambda *args: (frames, 30.0))
    root = tmp_path / "project"
    analyze(tmp_path / "clip.mp4", 0, 5, root,
            AnalyzeOptions(bg_override="#ffffff", refine=False, use_ecc=False), ocr=lambda frame: next(readings))
    scene, _ = current_scene(root, "s1")
    sprite, shape = sorted(scene.elements, key=lambda element: element.visible[0])
    source, target = (sprite, shape) if shape_is_target else (shape, sprite)
    reassign_id(root, "s1", (0, 5), source.id, target.id)
    merged_scene, _ = current_scene(root, "s1")
    merged, = merged_scene.elements
    assert merged.id == target.id and merged.kind == "sprite" and merged.visible == (0, 5)
    assert "reveal" not in merged.tracks
    with np.load(scene_dir(root, "s1") / merged.raw) as measured:
        assert measured["raw"].shape == (6, 9)
        np.testing.assert_array_equal(measured["raw"][:, 8], [1.0] * 6)
