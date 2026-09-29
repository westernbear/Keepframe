import cv2
import numpy as np

from keepframe.analyze.shots import boundary_digest, detect_boundaries, scene_layout, validate_scenes


def test_detects_cut_fade_slide_and_unknown():
    red = np.full((80, 120, 3), (255, 0, 0), np.uint8)
    blue = np.full((80, 120, 3), (0, 0, 255), np.uint8)
    cut = np.stack([red] * 5 + [blue] * 5)
    assert detect_boundaries(cut)[0].transition == "cut"

    fade = np.stack([np.full_like(red, value) for value in [0, 0, 0, 40, 80, 120, 160, 200, 240, 240, 240]])
    assert detect_boundaries(fade)[0].transition == "fade"

    rng = np.random.default_rng(7)
    texture = cv2.GaussianBlur(rng.integers(0, 256, (120, 160, 3), np.uint8), (9, 9), 0)
    slide = np.stack([
        cv2.warpAffine(texture, np.float32([[1, 0, dx], [0, 1, 0]]), (160, 120), borderMode=cv2.BORDER_REFLECT)
        for dx in [0, 0, 0, 4, 8, 12, 16, 20, 24, 24, 24]
    ])
    assert detect_boundaries(slide)[0].transition == "slide"

    unknown = np.stack([red] * 3 + [rng.integers(0, 256, red.shape, np.uint8) for _ in range(5)] + [blue] * 3)
    assert detect_boundaries(unknown)[0].transition == "unknown"


def test_scene_layout_keeps_short_scenes_and_digests_edits():
    first = np.full((32, 32, 3), (255, 0, 0), np.uint8)
    second = np.full((32, 32, 3), (0, 0, 255), np.uint8)
    scenes, transitions, warnings = scene_layout(np.stack([first] * 4 + [second] * 8), global_start=10)
    assert scenes == [{"id": "s1", "frames": [10, 13]}, {"id": "s2", "frames": [14, 21]}]
    assert transitions[0]["transition"] == "cut"
    assert warnings == [{"code": "short_scene", "scene": "s1", "frames": 4, "requires_ack": True}]
    validate_scenes(scenes, 10, 21)
    assert boundary_digest(scenes) != boundary_digest([{"id": "s1", "frames": [10, 14]}, {"id": "s2", "frames": [15, 21]}])
