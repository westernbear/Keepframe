"""Task 9b: text that fades in (or out) takes its canonical and matting frames at full opacity, and its opacity track
is measured against that peak."""
import numpy as np
from keepframe.analyze import matting
from keepframe.analyze import text as text_module
from keepframe.analyze.text import TextBox, TextTrack, text_props
from keepframe.ir.colour import hex_to_rgb8
from keepframe.ir.schema import Background, Keyframe, Track
from keepframe.ir.tracks import eval_props
from keepframe.qa.metrics import glyph_colour_delta
from tests.test_matting import _disc, _frames, _glyphs, _grad, _plate, _scene, _split

FILL = "#a8001c"
N = 24
FADES = {"in": [Keyframe(t=0, v=0.0), Keyframe(t=10, v=1.0)],      # 0 → 1 over frames 0–10, then held
         "out": [Keyframe(t=13, v=1.0), Keyframe(t=23, v=0.0)],    # held, then 1 → 0 over frames 13–23
         None: [Keyframe(t=0, v=1.0)]}


def _case(root, fade, n=N, generous=True):
    """A #a8001c title held at the centre of a pink gradient, with an opacity fade, and the OCR track a reader gives:
    from the frame it reaches 0.2 opacity, the faint frames (below 0.6) boxed 3 px wider a side (so today's canonical
    frame, the largest box, is a faint one); a constant title gets 1 px of box jitter on a few frames."""
    rgba = _glyphs("Big Sale", hex_to_rgb8(FILL), scale=1.4, thick=5)
    h, w = rgba.shape[:2]
    tracks = {"x": Track(keys=[Keyframe(t=0, v=160.0)]), "y": Track(keys=[Keyframe(t=0, v=90.0)]),
              "opacity": Track(keys=FADES[fade])}
    bg = Background(kind="gradient", gradient=_grad("#ffe1ea", "#ff6f91", 90.0))
    scene = _scene(root, [("t", rgba, tracks, 1)], bg, n)
    frames, plate = _frames(scene, root), _plate(bg)
    truth = np.array([eval_props(scene.elements[0], f)["opacity"] for f in range(n)])
    box = (160 - w // 2, 90 - h // 2, 160 + w // 2, 90 + h // 2)

    def ocr_box(f):
        g = 3 if generous and truth[f] < 0.6 else (1 if fade is None and f in (5, 9, 17) else 0)
        return (box[0] - g, box[1] - g, box[2] + g, box[3] + (g if fade is not None else 0))

    track = TextTrack(1, {f: TextBox(f, "Big Sale", ocr_box(f), 0.99) for f in range(n) if truth[f] >= 0.2}, "Big Sale")
    return frames, plate, truth, track


def _check_full_opacity(root, fade):
    frames, plate, truth, track = _case(root, fade)
    seen = sorted(track.boxes)
    widest = max(track.boxes, key=lambda f: text_module._box_area(track.boxes[f]))
    assert truth[widest] < 0.6   # the fixture's premise: today's rule picks a faint frame
    raw, canon, cf, _, _ = text_props(track, frames, plate.rgb, N, 0, infer_font=False, plate=plate.image)
    assert truth[cf] == 1.0
    op = raw[seen, 7]
    assert np.abs(op - truth[seen]).max() < 0.1   # the ramp, against the peak
    assert op[truth[seen] == 1.0].min() >= 0.98   # the hold
    rgba, meta = matting.texture_v2(raw, canon[..., 3] > 127, frames, plate, pad=0, kind_hint="text", ref_frame=cf)
    F, a = _split(rgba)
    assert glyph_colour_delta(F, a, FILL) < 3
    assert all(truth[f] >= 0.95 for f in meta.frames)


def test_faded_in_title_takes_cf_at_full_opacity(tmp_path):
    _check_full_opacity(tmp_path, "in")


def test_fade_out_title_same(tmp_path):
    _check_full_opacity(tmp_path, "out")


def test_constant_title_unchanged(tmp_path, monkeypatch):
    frames, plate, truth, track = _case(tmp_path, None)
    got = text_props(track, frames, plate.rgb, N, 0, infer_font=True, plate=plate.image)
    monkeypatch.setattr(text_module, "FULL_OPACITY", 0.0)   # every frame counts as full: today's rule
    want = text_props(track, frames, plate.rgb, N, 0, infer_font=True, plate=plate.image)
    assert got[2] == want[2] == 5   # the largest box (the first of the jittered ones)
    assert np.array_equal(got[0], want[0], equal_nan=True) and np.array_equal(got[1], want[1])
    assert got[3] == want[3] and got[4] == want[4]


def test_clutter_does_not_take_the_canonical_frame(tmp_path):
    """A held title with a dark disc (more contrast than the glyphs) behind it in 5 of 24 frames: those frames are
    not its full-opacity state, so the largest box (a clean frame) stays the canonical frame."""
    rgba = _glyphs("Big Sale", hex_to_rgb8(FILL), scale=1.4, thick=5)
    h, w = rgba.shape[:2]
    still = {"x": Track(keys=[Keyframe(t=0, v=160.0)]), "y": Track(keys=[Keyframe(t=0, v=90.0)])}
    disc = {"x": Track(keys=[Keyframe(t=0, v=190.0)]), "y": Track(keys=[Keyframe(t=0, v=90.0)])}
    bg = Background(kind="gradient", gradient=_grad("#ffe1ea", "#ff6f91", 90.0))
    scene = _scene(tmp_path, [("d", _disc(44, rgb=(12, 12, 24), accent=(12, 12, 24)), disc, 0), ("t", rgba, still, 1)], bg, N)
    scene = scene.model_copy(update={"elements": [scene.elements[0].model_copy(update={"visible": (3, 7)}), scene.elements[1]]})
    frames, plate = _frames(scene, tmp_path), _plate(bg)
    box = (160 - w // 2, 90 - h // 2, 160 + w // 2, 90 + h // 2)
    track = TextTrack(1, {f: TextBox(f, "Big Sale", (box[0], box[1], box[2] + (1 if f == 12 else 0), box[3]), 0.99) for f in range(N)},
                      "Big Sale")
    cf = text_props(track, frames, plate.rgb, N, 0, infer_font=False, plate=plate.image)[2]
    assert cf == 12, cf


def test_full_opacity_frame_fails_soft(tmp_path, monkeypatch):
    """An error in the measure keeps today's canonical frame (the largest box) and its opacity, with a report message."""
    from keepframe.analyze.pipeline import _texture_messages
    frames, plate, truth, track = _case(tmp_path, "in")
    monkeypatch.setattr(text_module, "FULL_OPACITY", 0.0)
    want = text_props(track, frames, plate.rgb, N, 0, infer_font=False, plate=plate.image)
    monkeypatch.undo()

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(text_module, "full_opacity_frame", boom)
    notes: list[str] = []
    got = text_props(track, frames, plate.rgb, N, 0, infer_font=False, plate=plate.image, notes=notes)
    assert got[2] == want[2] and truth[got[2]] < 0.6 and np.array_equal(got[0], want[0], equal_nan=True)
    assert notes == [text_module.FULL_OPACITY_NOTE]                         # a code, never the exception (R52)
    assert _texture_messages({"t1": {"fade_note": notes[0]}}, {"t1": "e1"}) == [f"e1: {notes[0]}"]
