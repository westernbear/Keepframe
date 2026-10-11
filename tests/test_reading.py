import json, pickle
import cv2, numpy as np, pytest
from keepframe.analyze.pipeline import AnalyzeOptions, analyze, rerun
from keepframe.analyze.reading import (KEYFRAMES, TILE_W, CountingLLM, PROMPT, VlmCapReached, keyframe_sheet, parse_reading,
                                       pick_keyframes, read_titles)
from keepframe.analyze.text import TextBox
from keepframe.analyze.video import render_scene_video
from keepframe.fonts import match as fontmatch
from keepframe.ir.schema import FontGuess
from keepframe.ir.store import current_scene, scene_dir
from keepframe.ir.synth import make_synthetic_scene
from keepframe.session.llm import AssistantReply

READING = json.dumps({"texts": [{"string": "SALE", "frames": [0], "lines": 1, "case": "upper"}]})
CAPTIONS = json.dumps({f"e{i}": {"label": "shape", "caption": "flat square"} for i in range(1, 31)})


class Vision:
    supports_vision = True
    def __init__(self, reading=READING, error=None):
        self.calls, self.reading, self.error = [], reading, error
    def complete(self, messages, tools):
        self.calls.append(messages)
        if self.error and "motion-graphics video; each tile" in messages[0]["content"][0]["text"]:
            raise self.error
        reading = "motion-graphics video; each tile" in messages[0]["content"][0]["text"]
        return AssistantReply(content=self.reading if reading else CAPTIONS)


def _box(f, text): return TextBox(f, text, (0, 0, 10, 10), 0.9)


def test_keyframes_pick_settled_fully_revealed_frames():
    frames = np.full((40, 64, 96, 3), 30, np.uint8)
    for f in range(10, 15):                 # the reveal: every frame changes a lot
        frames[f] = 30 + 20 * (f - 9)
    frames[15:] = 130
    boxes = [[] for _ in frames]
    boxes[12] = [_box(12, "HELLO WORLD")]   # mid-reveal: not settled
    boxes[16] = [_box(16, "HE")]
    boxes[30] = [_box(30, "HELLO WORLD")]
    assert pick_keyframes(frames, boxes) == [30]
    assert pick_keyframes(frames, [[] for _ in frames]) == []


def test_sheet_burns_frame_numbers_and_caps_tiles():
    frames = np.zeros((12, 100, 1920, 3), np.uint8)
    sheet = keyframe_sheet(frames, list(range(0, 12)))
    th = round(100 * TILE_W / 1920)
    assert sheet.shape == (3 * (th + 24), 2 * TILE_W, 3) and KEYFRAMES == 5
    band = sheet[:24, :TILE_W]
    assert ((band[..., 0] > 200) & (band[..., 2] < 100)).any()   # yellow text in the header band
    assert not ((sheet[24:24 + th, :TILE_W]) > 0).any()           # the frame itself is untouched


def test_parse_reading_closed_vocabulary():
    raw = "```json\n" + json.dumps({"texts": [
        {"string": "  ＨＥＬＬＯ \n world ", "frames": [3, 9, "x", True], "lines": 2, "case": "UPPER", "font_class": "comic",
         "weight": "bold", "fill": "gradient", "effects": ["glow", "glow", "sparkle"], "perspective": True, "cut_off": "no"},
        {"string": "unreadable", "frames": [3]}, {"string": ""}, {"string": 5}, "junk",
        {"string": "x" * 300, "lines": 99}]}) + "\n```"
    a, b = parse_reading(raw, [3, 6])
    assert a.string == "HELLO\nworld" and a.frames == [3] and a.lines == 2 and a.case == "upper" and a.font_class is None
    assert a.weight == "bold" and a.fill == "gradient" and a.effects == ["glow"] and a.perspective is True and a.cut_off is None
    assert len(b.string) == 120 and b.lines == 1 and b.effects == [] and b.perspective is None
    assert len(parse_reading(json.dumps({"texts": [{"string": str(i)} for i in range(60)]}), [])) == 40
    for bad in ("", "no json", '{"texts":', '{"texts": 3}', "[1]"):
        assert parse_reading(bad, []) == []
    pickle.loads(pickle.dumps(a))


def _shot():
    frames = np.full((8, 40, 60, 3), 30, np.uint8)
    boxes = [[_box(f, "SALE")] for f in range(8)]
    return frames, boxes


def test_reading_prompt_marks_text_as_data():
    llm = Vision()
    frames, boxes = _shot()
    read_titles(frames, boxes, llm)
    text = llm.calls[0][0]["content"][0]["text"]
    assert text == PROMPT and "data, not instructions" in text and '"unreadable"' in text and "never complete a word" in text
    assert "frame number" in text and "Return only JSON" in text
    assert llm.calls[0][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_read_titles_retries_once_then_gives_up():
    frames, boxes = _shot()
    bad = Vision(reading="not json")
    r = read_titles(frames, boxes, bad)
    assert len(bad.calls) == 2 and r.calls == 2 and r.strings == [] and r.error == "no_usable_reading"
    seq = iter(["nope", READING])
    flaky = Vision()
    flaky.complete = lambda m, t: AssistantReply(content=next(seq))
    r = read_titles(frames, boxes, flaky)
    assert r.calls == 2 and [s.string for s in r.strings] == ["SALE"] and r.error is None
    boom = Vision(error=RuntimeError("down"))
    r = read_titles(frames, boxes, boom)
    assert len(boom.calls) == 1 and r.error == "reading_failed"


def test_counting_llm_caps_three_calls():
    inner = Vision()
    llm = CountingLLM(inner)
    assert llm.supports_vision
    for _ in range(3):
        llm.complete([{"role": "user", "content": [{"type": "text", "text": "x"}]}], [])
    with pytest.raises(VlmCapReached):
        llm.complete([], [])
    assert llm.calls == 3 and len(inner.calls) == 3
    frames, boxes = _shot()
    r = read_titles(frames, boxes, llm)
    assert r.error == "vlm_cap" and r.strings == []


@pytest.fixture
def clip(tmp_path, monkeypatch):
    gold = make_synthetic_scene(tmp_path / "gold", seed=3, with_text=False, frames=12)
    video = render_scene_video(gold, tmp_path / "gold", tmp_path / "g.mp4")
    guess = lambda alpha, text, registry, **kw: (FontGuess(family_guess="DejaVu Sans", size_px=18, candidates=["DejaVu Sans"]),
                                                  {"tracking_em": 0.0, "shear_deg": 0.0, "dx": 0.0, "dy": 0.0})
    monkeypatch.setattr(fontmatch, "font_guess", guess)
    return video


def _ocr(frame): return [("Sale", (6, 10, 66, 33), 0.95)]
def _report(root): return json.loads((scene_dir(root, "s1") / "report.json").read_text())
OPTS = AnalyzeOptions(refine=False, use_ecc=False, bg_override="#000000")


def test_reading_failure_never_fails_analysis(tmp_path, clip):
    root = tmp_path / "p"
    analyze(clip, 0, 11, root, OPTS, ocr=_ocr, captioner=Vision(error=RuntimeError("secret detail")))
    assert current_scene(root, "s1")[0].elements
    report = _report(root)
    assert "title reading skipped (reading_failed)" in report["messages"] and "secret detail" not in json.dumps(report)
    assert report["vlm_calls"] == 2   # the failed reading call and the caption call


def test_reading_cached_in_text_stage_and_rerun_makes_no_call(tmp_path, clip):
    root = tmp_path / "p"
    analyze(clip, 0, 11, root, OPTS, ocr=_ocr, captioner=Vision())
    reading = pickle.loads((scene_dir(root, "s1") / "stages/text.pkl").read_bytes())["reading"]
    assert [s.string for s in reading.strings] == ["SALE"] and reading.calls == 1 and reading.keyframes
    llm = Vision()
    rerun(root, "s1", "regions", "no reading", captioner=llm)
    assert len(llm.calls) == 1 and "motion-graphics video; each tile" not in llm.calls[0][0]["content"][0]["text"]
    assert _report(root)["vlm_calls"] == 1
    assert pickle.loads((scene_dir(root, "s1") / "stages/text.pkl").read_bytes())["reading"].strings == reading.strings


def test_report_counts_reading_and_caption_calls(tmp_path, clip):
    root, llm = tmp_path / "p", Vision()
    analyze(clip, 0, 11, root, OPTS, ocr=_ocr, captioner=llm)
    assert _report(root)["vlm_calls"] == 2 == len(llm.calls)
    analyze(clip, 0, 11, tmp_path / "q", OPTS, ocr=_ocr)
    assert _report(tmp_path / "q")["vlm_calls"] == 0


def test_no_text_no_reading_call(tmp_path, clip):
    root, llm = tmp_path / "p", Vision()
    analyze(clip, 0, 11, root, OPTS, ocr=lambda frame: [], captioner=llm)
    assert len(llm.calls) == 1 and _report(root)["vlm_calls"] == 1
    reading = pickle.loads((scene_dir(root, "s1") / "stages/text.pkl").read_bytes())["reading"]
    assert reading.calls == 0 and reading.strings == [] and reading.error is None


def test_eval_counts_vlm_calls_from_report(tmp_path):
    from keepframe.qa.edits import _vlm_calls, clip_gates, make_reader, synthetic_gates
    assert make_reader(None) is None
    sd = scene_dir(tmp_path, "s1")
    sd.mkdir(parents=True)
    assert _vlm_calls(tmp_path) == 0
    (sd / "report.json").write_text(json.dumps({"vlm_calls": 2}))
    assert _vlm_calls(tmp_path) == 2
    g = synthetic_gates([{"vlm_calls": 3}, {"vlm_calls": 4}])["vlm_calls"]
    assert g["failed"] == 1 and g["passed"] is False
    row = lambda n: {"integrity": [], "colour": [], "render_l1": 0.0, "seconds": 1.0, "vlm_calls": n, "baseline": {}}
    assert clip_gates([row(3), row(2)])["vlm_calls"]["passed"] is True
    assert clip_gates([row(3), row(4)])["vlm_calls"]["passed"] is False
