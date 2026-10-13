import cv2
import numpy as np

from keepframe.analyze.shots import auto_scenes, boundary_digest, detect_boundaries, validate_scenes

RNG = np.random.default_rng(7)


def _card(colour, text: str, size=(120, 200), at=(20, 70), scale=1.0):
    """A coloured card with a line of text: structure that a cut replaces and a move keeps."""
    im = np.full((*size, 3), colour, np.uint8)
    ink = (255, 255, 255) if sum(colour) < 380 else (20, 20, 20)
    cv2.putText(im, text, at, cv2.FONT_HERSHEY_SIMPLEX, 0.9 * scale, ink, 2, cv2.LINE_AA)
    return im


def test_cut_lands_at_the_spike_even_next_to_motion():
    a = [_card((20, 20, 60), "Ship faster", at=(20 + 3 * i, 70)) for i in range(8)]   # sliding, then cut
    b = [_card((240, 240, 245), "Plan today", at=(30, 70)) for _ in range(8)]
    assert [x.frame for x in detect_boundaries(np.stack(a + b))] == [8]


def test_solid_colour_cut():
    red, blue = np.full((80, 120, 3), (255, 0, 0), np.uint8), np.full((80, 120, 3), (0, 0, 255), np.uint8)
    assert [b.transition for b in detect_boundaries(np.stack([red] * 5 + [blue] * 5))] == ["cut"]


def test_not_cuts_fade_slide_flash_zoom_build_swap():
    fade = np.stack([np.full((80, 120, 3), v, np.uint8) for v in [0, 0, 0, 40, 80, 120, 160, 200, 240, 240, 240]])
    texture = cv2.GaussianBlur(RNG.integers(0, 256, (120, 160, 3), np.uint8), (9, 9), 0)
    slide = np.stack([cv2.warpAffine(texture, np.float32([[1, 0, dx], [0, 1, 0]]), (160, 120), borderMode=cv2.BORDER_REFLECT)
                      for dx in [0, 0, 0, 4, 8, 12, 16, 20, 24, 24, 24]])
    dark, light = _card((20, 20, 60), "Ship faster"), _card((235, 235, 240), "Ship faster")
    flash = np.stack([dark] * 6 + [light] * 4 + [dark] * 6)                      # an inverse flash returns: one shot
    zoom = np.stack([_card((20, 20, 60), "Go", at=(40, 90), scale=2.4)] * 6 + [_card((20, 20, 60), "Go", at=(80, 70))] * 6)
    build = np.stack([_card((20, 20, 60), "Ship")] * 6 + [_card((20, 20, 60), "Ship faster")] * 6)   # a word is added
    ground = _card((235, 235, 240), "Plan today", at=(20, 50))
    cv2.rectangle(ground, (10, 80), (190, 112), (124, 63, 228), -1)   # a footer bar that stays through the swap
    cv2.putText(ground, "Pulse Labs", (20, 104), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
    swap = ground.copy(); swap[25:60] = 235
    cv2.putText(swap, "Grow team", (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (20, 20, 20), 2, cv2.LINE_AA)   # title swap, footer stays
    for clip in (fade, slide, flash, zoom, build, np.stack([ground] * 6 + [swap] * 6)):
        assert detect_boundaries(clip) == []


def test_auto_scenes_split_at_cuts_and_merge_short_ones():
    a, b = _card((20, 20, 60), "Ship faster"), _card((240, 240, 245), "Plan today")
    scenes, transitions = auto_scenes(np.stack([a] * 8 + [b] * 10), start=10)
    assert scenes == [{"id": "s1", "frames": [10, 17]}, {"id": "s2", "frames": [18, 27]}]
    assert transitions[0]["from"] == "s1" and transitions[0]["to"] == "s2" and transitions[0]["transition"] == "cut"
    validate_scenes(scenes, 10, 27)
    assert auto_scenes(np.stack([a] * 3 + [b] * 10))[0] == [{"id": "s1", "frames": [0, 12]}]   # a 3-frame scene is no scene
    assert boundary_digest(scenes) != boundary_digest([{"id": "s1", "frames": [10, 18]}, {"id": "s2", "frames": [19, 27]}])
