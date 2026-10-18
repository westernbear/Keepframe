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


def test_gradual_transitions_split_when_the_picture_is_replaced():
    a, b = _card((240, 236, 228), "Every Build"), _card((228, 224, 212), "Clear Grow")   # one ground: only the cover splits

    def blocks(base, k):   # four orange columns drop in over the picture
        im = base.copy()
        for c in range(4):
            im[:int(np.clip((k - c) / 5, 0, 1) * 120), c * 50:(c + 1) * 50] = (255, 90, 40)
        return im

    def covered(x, y):
        return np.stack([x] * 6 + [blocks(x, k) for k in range(1, 9)] + [blocks(y, k) for k in range(8, 0, -1)] + [y] * 6)

    assert [(x.frame, x.transition) for x in detect_boundaries(covered(a, b))] == [(13, "transition")]
    assert detect_boundaries(covered(a, a)) == []   # the same title comes back: the cover splits nothing
    dark, light = _card((15, 15, 40), "Ship faster"), _card((240, 240, 245), "Plan today")
    yy, xx = np.mgrid[:120, :200]
    circle = [np.where(((xx - 100) ** 2 + (yy - 60) ** 2 < (15 * r) ** 2)[..., None], light, dark).astype(np.uint8) for r in range(1, 9)]
    assert [x.transition for x in detect_boundaries(np.stack([dark] * 6 + circle + [light] * 6))] == ["transition"]


def test_cut_under_a_soft_glow():
    yy, xx = np.mgrid[:120, :200]
    blob = np.exp(-(((xx - 130) / 55.0) ** 2 + ((yy - 90) / 25.0) ** 2))[..., None]

    def frame(text, k, scale=0.8, at=(14, 34)):   # a glow pulsing on a dark ground with a title clear of it
        im = (np.array((12, 12, 30)) + blob * (0.8 + 0.04 * (k % 5)) * np.array((40, 160, 120))).astype(np.uint8)
        cv2.putText(im, text, at, cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), 2, cv2.LINE_AA)
        return im

    small, big = [frame("Ship faster", k) for k in range(8)], [frame("Go", k, 1.8, (60, 48)) for k in range(8, 16)]
    assert [x.frame for x in detect_boundaries(np.stack(small + big))] == [8]   # the glow goes on, the title is replaced
    assert detect_boundaries(np.stack(small + [frame("Ship faster", k) for k in range(8, 16)])) == []


def test_auto_scenes_split_at_cuts_and_merge_short_ones():
    a, b = _card((20, 20, 60), "Ship faster"), _card((240, 240, 245), "Plan today")
    scenes, transitions = auto_scenes(np.stack([a] * 8 + [b] * 10), start=10)
    assert scenes == [{"id": "s1", "frames": [10, 17]}, {"id": "s2", "frames": [18, 27]}]
    assert transitions[0]["from"] == "s1" and transitions[0]["to"] == "s2" and transitions[0]["transition"] == "cut"
    validate_scenes(scenes, 10, 27)
    assert auto_scenes(np.stack([a] * 3 + [b] * 10))[0] == [{"id": "s1", "frames": [0, 12]}]   # a 3-frame scene is no scene
    assert boundary_digest(scenes) != boundary_digest([{"id": "s1", "frames": [10, 18]}, {"id": "s2", "frames": [19, 27]}])
