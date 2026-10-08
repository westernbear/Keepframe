import cv2
import numpy as np
import pytest

from keepframe.analyze.pipeline import AnalyzeOptions, _stage_sprites
from keepframe.analyze.regions import Region
from keepframe.analyze.sprites import estimate_opacity, sprite_props
from keepframe.analyze.text import TextBox, TextTrack, stroke_mask, text_props
from keepframe.analyze.tracking import ObjectTrack


@pytest.fixture
def local_plate_clip():
    h, w = 64, 128
    ramp = np.linspace(0, 200, w).astype(np.uint8)
    plate = np.broadcast_to(ramp[None, :, None], (h, w, 3)).copy()
    bg = tuple(int(v) for v in plate.mean((0, 1)))
    bbox = (88, 12, 124, 44)
    x0, y0, x1, y1 = bbox
    mask = np.zeros((y1 - y0, x1 - x0), np.uint8)
    cv2.putText(mask, "HI", (1, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.7, 1, 2, cv2.LINE_8)
    mask = mask.astype(bool)
    frames = np.repeat(plate[None], 3, axis=0)
    # Opaque observations supply the canonical foreground colour for the half-opacity frame.
    for f, alpha in enumerate((1.0, 0.5, 1.0)):
        crop = frames[f, y0:y1, x0:x1]
        crop[mask] = np.rint(crop[mask] * (1 - alpha) + 255 * alpha).astype(np.uint8)
    text = TextTrack(id=1, text="HI", boxes={f: TextBox(f, "HI", bbox, 0.99) for f in range(3)})
    regions = {
        f: Region(f, 1, tuple(float(v) for v in frames[f, y0:y1, x0:x1][mask].mean(0)),
                  bbox, int(mask.sum()), ((x0 + x1) / 2, (y0 + y1) / 2), mask.copy())
        for f in range(3)
    }
    return frames, plate, bg, mask, text, ObjectTrack(id=3, regions=regions)


def test_stroke_mask_uses_local_plate_colour(local_plate_clip):
    frames, plate, bg, expected, text, _ = local_plate_clip
    bbox = text.boxes[1].bbox
    actual = stroke_mask(frames[1], bbox, bg, plate=plate)
    iou = np.count_nonzero(actual & expected) / np.count_nonzero(actual | expected)
    assert iou >= 0.9
    mean_mask = stroke_mask(frames[1], bbox, bg)
    assert np.count_nonzero(mean_mask & expected) / np.count_nonzero(mean_mask | expected) < 0.9


def test_text_opacity_uses_local_plate_colour(local_plate_clip):
    frames, plate, bg, expected, text, _ = local_plate_clip
    raw, canon, cf, _, _ = text_props(text, frames, bg, len(frames), 0, infer_font=False, plate=plate)
    assert raw[1, 7] == pytest.approx(0.5, abs=0.1)
    assert raw[cf, 7] == pytest.approx(1.0)
    assert np.array_equal(canon[..., 3] > 127, expected)
    mean_raw = text_props(text, frames, bg, len(frames), 0, infer_font=False)[0]
    assert abs(mean_raw[1, 7] - 0.5) > 0.1


def test_sprite_opacity_uses_local_plate_colour(local_plate_clip):
    frames, plate, bg, _, _, obj = local_plate_clip
    opacity = estimate_opacity(frames[1], obj.regions[1], (255, 255, 255), bg, plate=plate)
    assert opacity == pytest.approx(0.5, abs=0.1)
    assert abs(estimate_opacity(frames[1], obj.regions[1], (255, 255, 255), bg) - 0.5) > 0.1
    raw, _, _ = sprite_props(obj, frames, bg, len(frames), 0, use_ecc=False, plate=plate)
    assert raw[1, 7] == pytest.approx(0.5, abs=0.1)


@pytest.mark.parametrize("bbox", [(-4, 8, 34, 42), (-4, 8, 124, 44), (100, 40, 140, 80), (140, 20, 150, 30)])
def test_stroke_mask_clips_frame_and_plate_together(local_plate_clip, bbox):
    frames, plate, bg, strokes, text, _ = local_plate_clip
    mask = stroke_mask(frames[1], bbox, bg, plate=plate)
    x0, y0, x1, y1 = bbox
    h, w = plate.shape[:2]
    expected_shape = (max(0, min(h, y1) - max(0, y0)), max(0, min(w, x1) - max(0, x0)))
    if 0 in expected_shape:
        assert mask.shape == (0, 0)
    else:
        assert mask.shape == expected_shape
        expected = np.zeros((h, w), bool)
        tx0, ty0, tx1, ty1 = text.boxes[1].bbox
        expected[ty0:ty1, tx0:tx1] = strokes
        assert np.array_equal(mask, expected[max(0, y0):min(h, y1), max(0, x0):min(w, x1)])


def test_text_opacity_defaults_when_observation_is_outside_frame(local_plate_clip):
    frames, plate, bg, _, text, _ = local_plate_clip
    text.boxes[1] = TextBox(1, "HI", (140, 20, 150, 30), 0.99)
    raw = text_props(text, frames, bg, len(frames), 0, infer_font=False, plate=plate)[0]
    assert raw[1, 7] == 1.0


@pytest.mark.parametrize("contrast", [False, True])
def test_local_opacity_handles_pixels_without_contrast(contrast):
    plate = np.full((4, 6, 3), 255, np.uint8)
    frame = plate.copy()
    if contrast:
        plate[:, 3:] = 0
        frame[:, 3:] = 128
    region = Region(0, 1, (255.0,) * 3, (0, 0, 6, 4), 24, (3, 2), np.ones((4, 6), bool))
    expected = 128 / 255 if contrast else 1.0
    assert estimate_opacity(frame, region, (255, 255, 255), (99, 99, 99), plate=plate) == pytest.approx(expected)
    region.mask[:] = False
    assert estimate_opacity(frame, region, (255, 255, 255), (99, 99, 99), plate=plate) == 1.0


def test_solid_plate_matches_colour_background(local_plate_clip):
    frames, _, _, _, text, obj = local_plate_clip
    bg = (32, 64, 96)
    plate = np.full_like(frames[0], bg)
    assert np.array_equal(stroke_mask(frames[1], text.boxes[1].bbox, bg, plate=plate),
                          stroke_mask(frames[1], text.boxes[1].bbox, bg))
    for measure, track, kwargs in ((text_props, text, {"infer_font": False}), (sprite_props, obj, {"use_ecc": False})):
        solid = measure(track, frames, bg, len(frames), 0, **kwargs)
        local = measure(track, frames, bg, len(frames), 0, plate=plate, **kwargs)
        assert np.allclose(solid[0], local[0], equal_nan=True)
        assert np.array_equal(solid[1], local[1])


def test_sprite_stage_passes_plate_to_text_shapes_and_objects(local_plate_clip, tmp_path, monkeypatch):
    frames, plate, bg, mask, text, obj = local_plate_clip
    monkeypatch.setattr("keepframe.analyze.text.font_candidates", lambda *args: [])
    monkeypatch.setattr("keepframe.analyze.text.font_family_guess", lambda *args: "sans-serif")
    shape = TextTrack(id=2, text=text.text, boxes=text.boxes.copy())
    props = _stage_sprites(frames, bg, [text], [shape], [obj],
                           AnalyzeOptions(refine=False, use_ecc=False), tmp_path, len(frames), plate=plate)
    for key in ("t1", "s2", "o3"):
        assert props[key]["raw"][1, 7] == pytest.approx(0.5, abs=0.1)
        pad, (h, w) = props[key]["texture_meta"]["padding"], props[key]["canon"].shape[:2]   # textures v2: padded
        assert np.array_equal(props[key]["canon"][pad:h - pad, pad:w - pad, 3] > 127, mask)
