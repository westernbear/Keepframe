"""Task 10 (Stage B): a split or tilted title becomes one text prop in the analysis — plane raw, rectified canon, the
string re-read by OCR and reconciled with the VLM's, fragments out of the sprite list, matting skipped when tilted.
Synthetic scenes through `analyze`, with a frame-indexed fake OCR and a fake VLM."""
import json, pickle, shutil
import cv2, numpy as np, pytest
from keepframe.analyze import pipeline
from keepframe.analyze.pipeline import AnalyzeOptions, analyze, fallback_ids, rerun
from keepframe.analyze.reading import parse_reading
from keepframe.analyze.titles import _perspective, _reconcile, _vlm_line, fragment, merge_title
from keepframe.fonts import match as fontmatch
from keepframe.fonts.raster import render_styled
from keepframe.ir.schema import PROPS, Background, Canonical, Element, FontGuess, Keyframe, Scene, Track
from keepframe.ir.store import current_scene, scene_dir
from keepframe.ir.synth import make_reference_scene, render_frames
from keepframe.ir.tracks import element_bbox, eval_props, eval_track
from keepframe.review.corrections import add_bbox_prompt, validate_correction_targets
from keepframe.review.overlay import frame_overlay, read_manifest
from keepframe.session.llm import AssistantReply

OPTS = AnalyzeOptions(refine=False, use_ecc=False, generate_3d=False)
CAPTIONS = json.dumps({f"e{i}": {"label": "shape", "caption": "flat"} for i in range(1, 31)})
TITLE, SUB = "Grand Opening", "This Friday only"   # seed 16's title and subtitle


class Vision:
    """A fake VLM: the reading prompt gets `strings`, any other prompt (captions) a caption per element."""
    supports_vision = True

    def __init__(self, *strings):
        self.calls, self.strings = [], strings

    def complete(self, messages, tools):
        self.calls.append(messages)
        reading = "motion-graphics video; each tile" in messages[0]["content"][0]["text"]
        return AssistantReply(content=json.dumps({"texts": [{"string": s} for s in self.strings]}) if reading else CAPTIONS)


class Ocr:
    """Reads the truth's visible texts (opacity > 0.3) by frame. `split`: the first title as its words with its last
    glyph missed (a mover crossing behind splits it so in RapidOCR; a missed glyph becomes a sprite); `typo` replaces
    the first title's read. An image that is not a frame (the merge's re-read of the rectified title) reads `reread`."""

    def __init__(self, truth, root, frames, split=False, typo=None, reread=None):
        self.truth, self.split, self.typo, self.reread, self.rereads = truth, split, typo, reread, []
        self.index = {hash(f.tobytes()): i for i, f in enumerate(frames)}
        self.title = next(e for e in truth.elements if e.kind == "text")
        if split:
            ink = np.flatnonzero(cv2.imread(str(root / self.title.canonical.texture), cv2.IMREAD_UNCHANGED)[..., 3].max(0) > 0)
            runs = np.split(ink, np.flatnonzero(np.diff(ink) > 1) + 1)   # glyph runs
            w = int(np.argmax([b[0] - a[-1] for a, b in zip(runs, runs[1:])]))   # the word gap
            first, second = self.title.canonical.text.split()
            self.words = [(first, runs[0][0], runs[w][-1] + 1), (second[:-1], runs[w + 1][0], runs[-2][-1] + 1)]

    def __call__(self, img):
        f = self.index.get(hash(img.tobytes()))
        if f is None:
            self.rereads.append(img.shape)
            return [(self.reread, (2, 2, img.shape[1] - 2, img.shape[0] - 2), 0.9)] if self.reread else []
        out = []
        for e in self.truth.elements:
            if e.kind != "text" or not e.visible[0] <= f <= e.visible[1] or eval_props(e, f)["opacity"] <= 0.3:
                continue
            x0, y0, x1, y1 = (int(round(v)) for v in element_bbox(e, f, self.truth.size))
            if e is self.title and self.split:
                out += [(t, (x0 + int(c0), y0, x0 + int(c1), y1), 0.9) for t, c0, c1 in self.words]
            else:
                out.append((self.typo if e is self.title and self.typo else e.canonical.text, (x0, y0, x1, y1), 0.9))
        return out


def _video(truth, root) -> np.ndarray:
    return np.clip(np.rint(np.stack(render_frames(truth, root, range(truth.frames)))), 0, 255).astype(np.uint8)


def _analyse(tmp, truth, video, ocr, captioner=None):
    """`analyze` on the rendered frames with a font-matching stub that records what it was given."""
    jobs, root = [], tmp / "project"

    def guess(alpha, text, registry, **kw):
        jobs.append((text, alpha.shape, kw.get("centres")))
        return FontGuess(family_guess="Inter", size_px=40, candidates=["Inter"]), {"tracking_em": 0.0, "shear_deg": 0.0, "dx": 0.0, "dy": 0.0}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pipeline, "read_frames", lambda *a: (video, 30.0))
        mp.setattr(fontmatch, "font_guess", guess)
        analyze(tmp / "clip.mp4", 0, truth.frames - 1, root, OPTS, ocr=ocr, captioner=captioner)
    return root, jobs


def _split_scene(tmp):
    """30 frames of a title over a flat plate with a mover crossing behind it (as on seeds 4 and 6), its subtitle
    below, one sprite and the logo elsewhere."""
    truth = make_reference_scene(tmp / "truth", 16, plate="flat", mover=True, n_titles=2, n_sprites=1, frames=30)
    assert [e.canonical.text for e in truth.elements if e.kind == "text"] == [TITLE, SUB]
    return truth, _video(truth, tmp / "truth")


def _stages(root):
    sd = scene_dir(root, "s1")
    props = pickle.loads((sd / "stages/props.pkl").read_bytes())
    return props, json.loads((sd / "stages/ids.json").read_text()), current_scene(root, "s1")[0]


def _report(root):
    return json.loads((scene_dir(root, "s1") / "report.json").read_text())


def _titles(props):
    return [k for k in props if k.startswith("title_")]


def _stub(monkeypatch, ocr):
    """Reruns: the re-read engine is `ocr`, font matching a stub."""
    monkeypatch.setattr("keepframe.analyze.text.RapidOcr", lambda max_side=None: ocr)
    monkeypatch.setattr(fontmatch, "font_guess", lambda alpha, text, registry, **kw: (
        FontGuess(family_guess="Inter", size_px=40, candidates=["Inter"]), {"tracking_em": 0.0, "shear_deg": 0.0, "dx": 0.0, "dy": 0.0}))


@pytest.fixture(scope="module")
def split(tmp_path_factory):
    """Read as "Grand" + "Openin" (the "g" missed), no VLM; the re-read of the rectified title reads it whole."""
    tmp = tmp_path_factory.mktemp("split")
    truth, video = _split_scene(tmp)
    ocr = Ocr(truth, tmp / "truth", video, split=True, reread=TITLE)
    root, jobs = _analyse(tmp, truth, video, ocr)
    return root, truth, ocr, jobs, tmp


@pytest.fixture(scope="module")
def read(tmp_path_factory):
    """The same split read, with a VLM reading both lines as one two-line string and a re-read that disagrees with it."""
    tmp = tmp_path_factory.mktemp("read")
    truth, video = _split_scene(tmp)
    ocr = Ocr(truth, tmp / "truth", video, split=True, reread="Grnd Qpcnm")
    llm = Vision(f"{TITLE}\n{SUB}")
    root, _ = _analyse(tmp, truth, video, ocr, llm)
    return root, ocr, llm


def test_split_title_becomes_one_text_element(split):
    root, truth, _, _, tmp = split
    props, ids, scene = _stages(root)
    assert len(_titles(props)) == 1, list(props)
    key = _titles(props)[0]
    p, el = props[key], scene.element(ids[key])
    assert p["kind"] == "text" and p["raw"].shape == (truth.frames, 11) and not p["plane"]
    assert sorted(e.canonical.text for e in scene.elements if e.kind == "text") == sorted([TITLE, SUB])
    f = truth.frames - 2                      # the element spans the whole title's ink, not one word
    title = truth.element("title1")
    ink = np.flatnonzero(cv2.imread(str(tmp / "truth" / title.canonical.texture), cv2.IMREAD_UNCHANGED)[..., 3].max(0) > 0)
    tx0 = element_bbox(title, f)[0]
    x0, _, x1, _ = element_bbox(el, f)
    assert x0 <= tx0 + ink[0] + 1 and x1 >= tx0 + ink[-1] - 1 and x1 - x0 < 1.3 * (ink[-1] - ink[0]), (x0, x1, tx0 + ink[[0, -1]])
    assert "rx" not in el.tracks and "ry" not in el.tracks


def test_fragments_leave_the_sprite_list(split):
    root, truth, _, _, tmp = split
    props, ids, scene = _stages(root)
    key = _titles(props)[0]
    frags = props[key]["fragments"]
    assert sorted(p["text"] for k, p in frags.items() if p["kind"] == "text") == ["Grand", "Openin"]
    assert any(k.startswith("o") for k in frags), list(frags)          # the missed "g", a glyph-sized sprite
    assert not set(frags) & set(props)
    def inside(eid, f):
        a, b = element_bbox(scene.element(eid), f), element_bbox(scene.element(ids[key]), f)
        return b[0] <= a[0] and b[1] <= a[1] and a[2] <= b[2] and a[3] <= b[3]

    for k in props:   # nothing else stays on the title (the mover passes behind it, the sprites over it)
        if not k.startswith(("_", "m", "title_")) and k in ids:
            assert not (inside(ids[k], truth.frames - 2) and inside(ids[k], truth.frames - 12)), k


def test_merged_title_style_and_font_get_the_full_string(split):
    root, _, _, jobs, _ = split
    props, ids, scene = _stages(root)
    key = _titles(props)[0]
    p = props[key]
    assert p["text"] == TITLE and p.get("style") and not p.get("style_error")
    assert len(p["style_info"]["centres"]) == len(TITLE.replace(" ", ""))   # one ink piece per glyph of the full string
    text, shape, centres = next(j for j in jobs if j[0] == TITLE)
    assert shape == p["canon"].shape[:2] and centres is not None and len(centres) == len(TITLE.replace(" ", ""))
    assert scene.element(ids[key]).canonical.font.family_guess == "Inter"


def test_merged_title_shows_in_the_review_overlay(split):
    """Final review I1: the fragments' observations are the title's, so review shows its lane and recognised text."""
    root = split[0]
    props, ids, _ = _stages(root)
    scene, v = current_scene(root, "s1")
    eid = ids[_titles(props)[0]]
    assert next(o for o in read_manifest(root, v.analysis_file)["objects"] if o["id"] == eid)["intervals"]
    read = {r["text"] for f in range(scene.frames) for o in frame_overlay(root, scene, v, f)["objects"] if o["id"] == eid
            for r in o["ocr"]}
    assert read == {"Grand", "Openin"}


def test_without_vlm_text_comes_from_reread_and_is_flagged(split):
    root, _, ocr, _, _ = split
    props, ids, _ = _stages(root)
    key = _titles(props)[0]
    p = props[key]
    assert ocr.rereads                                               # the fragments alone read "Grand Openin"
    assert (p["text"], p["text_source"], p["text_unverified"]) == (TITLE, "ocr", "no_vlm")
    assert f"{ids[key]}: title text not verified (no_vlm)" in _report(root)["messages"]
    assert ids[key] in fallback_ids(props, ids)


def test_vlm_string_wins_and_disagreement_is_flagged(read):
    root, _, llm = read
    props, ids, scene = _stages(root)
    key = _titles(props)[0]
    p = props[key]
    assert (p["text"], p["text_source"], p["text_unverified"]) == (TITLE, "vlm", "vlm_ocr_disagree")
    assert scene.element(ids[key]).canonical.text == TITLE
    report = _report(root)
    assert f"{ids[key]}: title text not verified (vlm_ocr_disagree)" in report["messages"]
    assert ids[key] in fallback_ids(props, ids)
    assert report["vlm_calls"] == 2 == len(llm.calls)                 # the reading and the captions
    sub = next(p for k, p in props.items() if k.startswith("t") and p["text"] == SUB)
    assert not sub.get("text_unverified")


def test_vlm_string_corrects_an_unmerged_ocr_typo(tmp_path):
    truth, video = _split_scene(tmp_path)
    root, _ = _analyse(tmp_path, truth, video, Ocr(truth, tmp_path / "truth", video, typo="Grand 0pening"), Vision(TITLE, SUB))
    props, ids, scene = _stages(root)
    assert not _titles(props)
    key = next(k for k, p in props.items() if k.startswith("t") and p.get("text") == TITLE)
    assert props[key]["text_source"] == "vlm" and not props[key].get("text_unverified")
    assert scene.element(ids[key]).canonical.text == TITLE and ids[key] not in fallback_ids(props, ids)


def test_tilted_title_gets_rx_ry_and_skips_matting(tmp_path):
    """Synthetic seed 10 as the eval draws it: a yawed (ry −35° → −18°, rot −1.49°), gradient-faded title, sprites
    crossing it."""
    truth = make_reference_scene(tmp_path / "truth", 10, plate="flat", title="plane")
    video = _video(truth, tmp_path / "truth")
    root, _ = _analyse(tmp_path, truth, video, Ocr(truth, tmp_path / "truth", video, reread="lew Arivals"))
    props, ids, scene = _stages(root)
    assert len(_titles(props)) == 1, list(props)
    key = _titles(props)[0]
    p, el, title = props[key], scene.element(ids[key]), truth.element("title1")
    assert p["plane"] and {"rx", "ry"} <= set(el.tracks) and el.canonical.text == title.canonical.text
    assert (p["text_source"], p["text_unverified"]) == ("fragments", "no_vlm")   # R24: one re-read never beats the track
    assert "texture_meta" not in p and el.canonical.texture_meta is None   # textures v2 skipped (D14)
    assert set(np.unique(p["canon"][..., 3])) <= {0, 255}                 # the binary rectified texture
    settle = title.tracks["opacity"].keys[-1].t
    got, want = eval_track(el.tracks["ry"], settle), eval_track(title.tracks["ry"], settle)
    assert abs(got - want) <= 3.0, (got, want)


def test_upright_titles_stay_on_text_props(tmp_path):
    """R8: D12 at 0.8° convergence leaves upright titles (ig1-like) on the text_props path."""
    size, n, els = (960, 400), 8, []
    for i, (text, px, y) in enumerate((("Every day, ideas are born", 44, 130), ("Weekend", 64, 260))):
        rgba = render_styled([text], FontGuess(family_guess="Inter", weight=700, size_px=px, source="bundled"), "#ffffff", None)
        rgba = np.pad(rgba, ((0, rgba.shape[0] % 2), (0, rgba.shape[1] % 2), (0, 0)))
        (tmp_path / "truth" / "assets").mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(tmp_path / "truth" / "assets" / f"t{i}.png"), cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA))
        els.append(Element(id=f"t{i}", kind="text", canonical=Canonical(width=rgba.shape[1], height=rgba.shape[0],
                                                                        texture=f"assets/t{i}.png", text=text),
                           visible=(0, n - 1), z=Track(keys=[Keyframe(t=0, v=i + 1)]),
                           tracks={"x": Track(keys=[Keyframe(t=0, v=480.0)]), "y": Track(keys=[Keyframe(t=0, v=float(y))])}))
    truth = Scene(id="u", size=size, fps=30, frames=n, background=Background(value="#20304a"), elements=els)
    video = _video(truth, tmp_path / "truth")
    root, _ = _analyse(tmp_path, truth, video, Ocr(truth, tmp_path / "truth", video))
    props, _, _ = _stages(root)
    assert not _titles(props) and not [k for k, p in props.items() if isinstance(p, dict) and p.get("merge_note")]
    assert sorted(p["text"] for k, p in props.items() if k.startswith("t")) == ["Every day, ideas are born", "Weekend"]


def test_merge_failure_keeps_fragments_with_message(split, tmp_path, monkeypatch):
    root = tmp_path / "p"
    shutil.copytree(split[0], root)
    _stub(monkeypatch, split[2])

    def boom(*a, **k):
        raise RuntimeError("secret detail")

    monkeypatch.setattr("keepframe.analyze.titles.track_plane", boom)
    rerun(root, "s1", "sprites", note="merge fails")
    props, ids, scene = _stages(root)
    assert not _titles(props)
    kept = [k for k, p in props.items() if isinstance(p, dict) and p.get("merge_note") == "merge_failed"]
    assert {"Grand", "Openin"} <= {props[k].get("text") for k in kept}
    report = _report(root)
    for k in kept:
        assert f"{ids[k]}: title merge failed (merge_failed); kept as measured" in report["messages"]
    assert "secret detail" not in json.dumps(report) and {ids[k] for k in kept} <= fallback_ids(props, ids)


def test_rerun_from_sprites_remerges_with_cached_reading(read, tmp_path, monkeypatch):
    root = tmp_path / "p"
    shutil.copytree(read[0], root)
    _, before, _ = _stages(root)
    _stub(monkeypatch, read[1])
    rerun(root, "s1", "sprites", note="again")                       # no VLM: the reading comes from text.pkl
    props, ids, scene = _stages(root)
    key = _titles(props)[0]
    assert _report(root)["vlm_calls"] == 0
    assert ids == before and key in before
    assert (props[key]["text"], props[key]["text_source"]) == (TITLE, "vlm") and scene.element(ids[key]).canonical.text == TITLE


def test_reconcile_codes():
    assert _reconcile("", "Grand Opening", ["Grand", "Opening"], []) == ("Grand Opening", "fragments", "no_vlm")
    # R24 without a VLM (ig1 at 059a2b5): a re-read shorter than the longest fragment text, or of a single tilted
    # track, does not replace the fragments' string; one as long as it does
    assert _reconcile("inside uour", "inside your walls.", ["inside your walls.", "your"], []) == \
        ("inside your walls.", "fragments", "no_vlm")
    assert _reconcile("lew aproach", "New aproach", ["New aproach"], [], multi=False) == ("New aproach", "fragments", "no_vlm")
    assert _reconcile("Grand Opening", "Grand Openin", ["Grand", "Openin"], []) == ("Grand Opening", "ocr", "no_vlm")
    # R25: a fragment text that holds every other is the string, not the join (ig1: 'your' 9 px left of t6's centre)
    assert _reconcile("inside uour", "yourinside your walls.", ["inside your walls.", "your", "your walls."], []) == \
        ("inside your walls.", "fragments", "no_vlm")
    assert _reconcile("", "Join us foday", ["Join us foday", "Joivn us foday"], [])[0] == "Join us foday"   # most seen
    assert _reconcile("", "Grand Openin", ["Openin", "Grand"], [])[0] == "Grand Openin"   # none holds the other: join
    assert _reconcile("Grand Opening", "", ["Grand"], ["This Friday only"]) == ("Grand Opening", "ocr", "vlm_ocr_disagree")
    assert _reconcile("Grand 0pening", "", ["Grand"], [TITLE]) == (TITLE, "vlm", None)


def test_vlm_line_never_widens_the_title():
    """Final review I2: the reading keeps its line breaks and a VLM line replaces an OCR string only within LEN_TOL
    (25 %) of its compact length: the next line or an extra word never becomes the title's text; a typo still does."""
    two = [r.string for r in parse_reading(json.dumps({"texts": [{"string": " SUMMER  SALE \n UP TO 50% "}]}), [])]
    assert two == ["SUMMER SALE\nUP TO 50%"]
    assert _reconcile("SUMMER SALE", "SUMMER SALE", ["SUMMER", "SALE"], two) == ("SUMMER SALE", "vlm", None)
    assert _reconcile("SUMMER SALE", "SUMMER SALE", ["SUMMER", "SALE"], ["SUMMER SALE UP TO 50%"]) == \
        ("SUMMER SALE", "ocr", "vlm_ocr_disagree")
    assert _reconcile("New Arrivals", "New Arrivals", ["New", "Arrivals"], ["New Arrivals Now"]) == \
        ("New Arrivals", "ocr", "vlm_ocr_disagree")
    assert _reconcile("", "SUMMER SAL", ["SUMMER", "SAL"], two) == ("SUMMER SALE", "vlm", None)   # a missed glyph
    for text, strings in (("SUMMER SALE", ["SUMMER SALE 50%"]), ("New Arrivals", ["New Arrivals Now"])):
        assert _vlm_line(text, strings) is None   # the typo fix of an unmerged text
    assert _vlm_line("SUMMER SALE", ["SUMMER SALE\n50%"]) == "SUMMER SALE" and _vlm_line("Sal", ["Sale"]) == "Sale"


def test_bbox_correction_on_a_merged_title_goes_to_its_first_text_track(split, tmp_path, monkeypatch):
    """A merged title (`title_t<id>`) takes a bbox prompt through its first text track, and stays merged."""
    root = tmp_path / "p"
    shutil.copytree(split[0], root)
    _stub(monkeypatch, split[2])
    props, ids, _ = _stages(root)
    key = _titles(props)[0]
    first = int(key.removeprefix("title_t"))
    sd, f = scene_dir(root, "s1"), split[1].frames - 2
    box = next(t for t in pickle.loads((sd / "stages/text.pkl").read_bytes())["tracks"] if t.id == first).boxes[f].bbox
    bbox = (box[0], box[1], box[2] - 1, box[3])   # not larger: the track's canonical frame (largest box) stays
    validate_correction_targets(root, "s1", "bbox", {"object_id": ids[key]})
    add_bbox_prompt(root, "s1", f, bbox, ids[key])
    track = next(t for t in pickle.loads((sd / "stages/text.pkl").read_bytes())["tracks"] if t.id == first)
    assert track.boxes[f].bbox == bbox and track.boxes[f].source == "manual_box"
    assert _titles(_stages(root)[0]) == [key]


def test_turned_note_keeps_rx_ry_zero():
    """V3 (ig1 e26): a note turned 13° in the plane with a 23 px cap at 1920 wide: its line fits converge by noise
    only, so rot carries the turn and rx = ry = 0 (they read 46° and 58°)."""
    size, n = (1920, 1080), 3
    rgba = render_styled(["New aproach"], FontGuess(family_guess="Inter", weight=700, size_px=32, source="bundled"), "#202020", None)
    h, w = rgba.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), -13, 1.0)
    M[:, 2] += (300 - w / 2, 400 - h / 2)
    a = cv2.warpAffine(rgba[..., 3], M, size, flags=cv2.INTER_LINEAR)[..., None].astype(np.float32) / 255
    plate = np.full((size[1], size[0], 3), (250, 240, 200), np.uint8)
    frame = (plate * (1 - a) + np.float32([32, 32, 32]) * a).round().astype(np.uint8)
    frames = np.repeat(frame[None], n, 0)
    ys, xs = np.nonzero(a[..., 0] > 0.5)
    x0, y0, x1, y1 = xs.min(), ys.min(), xs.max() + 1, ys.max() + 1
    raw = np.full((n, 11), np.nan)
    raw[:] = [(x0 + x1) / 2, (y0 + y1) / 2, 1, 1, 0, 0, 0, 1, 1, 0, 0]
    p = {"raw": raw, "canon": np.dstack([frame[y0:y1, x0:x1], (a[y0:y1, x0:x1, 0] > 0.5).astype(np.uint8) * 255]),
         "kind": "text", "first": 0, "last": n - 1, "text": "New aproach", "font": None, "cf": 0}
    out = merge_title([fragment("t1", p, size)], {"t1": p}, frames, plate, ocr=None, strings=[], size=size, fps=30.0)
    assert not out["plane"] and not out["raw"][:, [PROPS.index("rx"), PROPS.index("ry")]].any()
    assert abs(abs(out["raw"][0, PROPS.index("rot")]) - 13) < 1.5, out["raw"][0]


def test_perspective_needs_a_cap_a_yaw_can_converge():
    """M1: lines converging 1.2° on a 20 px cap at 1920 wide are glyph noise (a 45° yaw converges them 0.43°)."""
    assert not _perspective({"rot_deg": 0.0, "converge_deg": 1.2, "cap_px": 20.0}, (1920, 1080))
    assert _perspective({"rot_deg": 0.0, "converge_deg": 1.2, "cap_px": 60.0}, (1920, 1080))
