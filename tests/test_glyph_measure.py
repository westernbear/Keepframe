"""Task 11 (Stage B): per-glyph delays measured on titles whose glyphs come in one by one (D15). A merged title or a
revealed text whose onsets spread ≥ 3 frames gets `glyphs`, and over the stagger the glyph α alone carries the
reveal (R14); a title that comes in as a whole stays in box mode."""
import cv2, numpy as np, pytest
from keepframe.analyze import pipeline
from keepframe.analyze.composite import composite_scene
from keepframe.analyze.pipeline import AnalyzeOptions, analyze
from keepframe.analyze.titles import GLYPH_SPREAD, glyph_reveal, merge_titles
from keepframe.fonts import match as fontmatch
from keepframe.ir.schema import FontGuess
from keepframe.ir.store import current_scene, scene_dir
from keepframe.ir.synth import make_reference_scene, make_text_texture, render_frames
from keepframe.ir.tracks import element_bbox, glyph_alphas, glyph_edges

SIZE, X0, Y0 = (200, 64), 20, 16


class PrefixOcr:
    """Reads a staggered title as RapidOCR does: its glyphs at α > 0.3 only, the box cut after the last of them."""

    def __init__(self, truth, frames):
        self.truth, self.index = truth, {hash(f.tobytes()): i for i, f in enumerate(frames)}

    def __call__(self, img):
        f, out = self.index.get(hash(img.tobytes())), []
        for e in self.truth.elements if f is not None else ():
            if e.kind != "text" or not e.visible[0] <= f <= e.visible[1]:
                continue
            g, text = e.canonical.glyphs, e.canonical.text
            k = int((glyph_alphas(g, f - e.visible[0]) > 0.3).sum())
            if k:
                x0, y0, _, y1 = (int(round(v)) for v in element_bbox(e, f))
                last = [i for i, ch in enumerate(text) if not ch.isspace()][k - 1]
                out.append((text[:last + 1], (x0, y0, x0 + glyph_edges(g, e.canonical.width)[k], y1), 0.9))
        return out


@pytest.fixture(scope="module")
def stagger(tmp_path_factory):
    """A seed-9 stagger title alone on a flat plate, analysed with the prefix OCR (a revealed text prop)."""
    tmp = tmp_path_factory.mktemp("stagger")
    truth = make_reference_scene(tmp / "truth", 9, plate="flat", n_sprites=0, logo=False, title="stagger")
    video = np.clip(np.rint(np.stack(render_frames(truth, tmp / "truth", range(truth.frames)))), 0, 255).astype(np.uint8)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pipeline, "read_frames", lambda *a: (video, 30.0))
        mp.setattr(fontmatch, "font_guess", lambda alpha, text, registry, **kw: (
            FontGuess(family_guess="Inter", size_px=40, candidates=["Inter"]), {"tracking_em": 0.0, "shear_deg": 0.0, "dx": 0.0, "dy": 0.0}))
        analyze(tmp / "clip.mp4", 0, truth.frames - 1, tmp / "project", AnalyzeOptions(refine=False, use_ecc=False, generate_3d=False),
                ocr=PrefixOcr(truth, video))
    return truth, tmp / "truth", current_scene(tmp / "project", "s1")[0], scene_dir(tmp / "project", "s1")


def _wiped(tex, reveal, opacity, n=30):
    """`n` frames of the RGBA `tex` at (X0, Y0) over a flat plate, column-wiped to `reveal(f)` and faded to `opacity(f)`,
    and its text prop as `text_props` makes a revealed text: the full box, reveal = the revealed fraction, cf the
    last frame (canon α binary)."""
    h, w = tex.shape[:2]
    plate = np.full((SIZE[1], SIZE[0], 3), 40, np.uint8)
    frames, raw = np.repeat(plate[None], n, 0), np.full((n, 9), np.nan)
    for f in range(n):
        r, o = reveal(f), opacity(f)
        if r > 0 and o > 0:
            a = tex[..., 3:] / 255.0 * o * (np.arange(w) < round(r * w))[None, :, None]
            frames[f, Y0:Y0 + h, X0:X0 + w] = np.rint(a * tex[..., :3] + (1 - a) * 40).astype(np.uint8)
            raw[f] = [X0 + w / 2, Y0 + h / 2, 1, 1, 0, 0, 0, o, r]
    rows = np.flatnonzero(np.isfinite(raw[:, 0]))
    canon = np.dstack([frames[-1, Y0:Y0 + h, X0:X0 + w], np.where(tex[..., 3] > 127, 255, 0).astype(np.uint8)])
    return frames, plate, {"raw": raw, "canon": canon, "cf": n - 1, "kind": "text", "first": int(rows[0]), "last": int(rows[-1]), "reveal": True}


def _text(tmp_path, text):
    make_text_texture(tmp_path / "t.png", text, 24, (255, 255, 255))
    return cv2.cvtColor(cv2.imread(str(tmp_path / "t.png"), cv2.IMREAD_UNCHANGED), cv2.COLOR_BGRA2RGBA)


def test_stagger_delays_recovered(stagger):
    truth, troot, scene, sd = stagger
    t = truth.element("title1")
    tg = t.canonical.glyphs
    el = next(e for e in scene.elements if e.kind == "text")
    g = el.canonical.glyphs
    assert g is not None and len(g.delays) == len(tg.delays)
    err = np.array(g.delays) + el.visible[0] - np.array(tg.delays) - t.visible[0]   # in absolute frames
    assert np.mean(np.abs(err) <= 1) >= 0.8 and abs(g.ramp - tg.ramp) <= 2
    # No double count: mid-stagger, the analysed title matches the truth's (the glyph α carries the reveal alone);
    # a whole-title fade or a wipe on top of the glyph mask would leave the late glyphs out.
    f = t.visible[0] + int(max(tg.delays) / 2)
    x0, y0, x1, y1 = (int(round(v)) for v in element_bbox(t, f))
    want = composite_scene(truth, troot, f)[y0:y1, x0:x1]
    box = scene.model_copy(deep=True)
    box.element(el.id).canonical.glyphs = None
    d = lambda s: float(np.abs(composite_scene(s, sd, f)[y0:y1, x0:x1] - want).mean())
    assert d(scene) <= 0.005 and d(box) >= 0.05   # box mode shows the whole title


def test_wipe_reveal_becomes_glyph_mode(tmp_path):
    tex = _text(tmp_path, "REVEAL")
    frames, plate, p = _wiped(tex, lambda f: np.clip((f - 4) / 16, 0, 1), lambda f: 1.0)
    p["raw"][p["first"]:20, 7] = np.linspace(0.05, 1, 20 - p["first"])   # what its core pixels read while partly revealed
    p["text"], notes = "REVEAL", []
    merge_titles({"t1": p}, frames, plate, ocr=None, reading=None, fps=30.0, notes=notes)
    g = p["glyphs"]
    assert notes == [] and len(g["x"]) == 6 and g["x"] == sorted(g["x"])
    onset = np.array(g["delays"]) + g["ramp"] / 2 + p["first"]   # glyph i is half in when the wipe passes its centre
    late = np.array(g["delays"]) > 0                              # (the first glyph's delay is clamped at 0)
    assert np.abs(onset - (4 + 16 * np.array(g["x"])))[late].max() <= 1 and onset[-1] - onset[0] >= GLYPH_SPREAD
    window = slice(p["first"], p["first"] + int(np.ceil(max(g["delays"]) + g["ramp"])) + 1)   # the glyphs carry the wipe there
    assert window.stop >= 20 and np.all(p["raw"][window, 7] == 1) and np.all(p["raw"][window, 8] == 1)


def test_held_title_stays_box_mode(tmp_path):
    tex = _text(tmp_path, "REVEAL")
    frames, plate, p = _wiped(tex, lambda f: float(f >= 4), lambda f: np.clip((f - 3) / 8, 0, 1))   # a whole-title fade
    p["text"], raw = "REVEAL", p["raw"].copy()
    merge_titles({"t1": p}, frames, plate, ocr=None, reading=None, fps=30.0, notes=[])
    assert "glyphs" not in p and np.array_equal(p["raw"], raw, equal_nan=True)


def test_unreadable_glyphs_use_uniform_columns(tmp_path):
    tex = np.zeros((20, 120, 4), np.uint8)
    tex[4:16, 10:110] = 255   # one blob of ink: five glyphs that cannot be told apart
    frames, plate, p = _wiped(tex, lambda f: np.clip((f - 4) / 16, 0, 1), lambda f: 1.0)
    g = glyph_reveal(p, frames, plate, SIZE, "ABCDE")
    assert g is not None and np.allclose(np.array(g["x"]) * 120, 10 + 20 * np.arange(5) + 10)
    assert np.all(np.diff(g["delays"]) > 0)
