"""`analyze.fonts` wrappers over the render-and-compare matcher (Task 10), and where analysis takes its font."""
import numpy as np
import pytest
from keepframe.analyze import fonts
from keepframe.analyze.fonts import font_candidates, font_family_guess
from keepframe.analyze.text import TextBox, TextTrack, text_props
from keepframe.fonts import match as fontmatch
from keepframe.fonts.raster import glyph_alpha
from keepframe.fonts.registry import FontRegistry
from keepframe.ir.schema import FontGuess

REG = FontRegistry()


def _stroke(family: str, text: str, size: float, weight: int = 400) -> np.ndarray:
    a = glyph_alpha([text], size, [REG.face(family, weight)], weight=weight) > 0.5
    ys, xs = np.nonzero(a)
    return a[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


@pytest.mark.parametrize("size", [40, 300])
def test_rendered_family_ranks_first(size):
    stroke = _stroke("Playfair Display", "Launch faster", size)
    candidates = font_candidates(stroke, "Launch faster", size)
    assert candidates[0] == "Playfair Display"
    assert len(candidates) == 3 and len(set(candidates)) == 3
    assert font_candidates(stroke, "Launch faster", size, k=1) == ["Playfair Display"]
    assert font_family_guess(stroke, "Launch faster", size, candidates) == "Playfair Display"


def test_candidates_are_registered_families():
    stroke = _stroke("Inter", "Grand Opening", 48, 600)
    assert set(font_candidates(stroke, "Grand Opening", 48)) <= set(REG.families())


def test_hangul_candidates_cover_the_text():
    stroke = _stroke("Pretendard", "지금 시작하기", 40)
    candidates = font_candidates(stroke, "지금 시작하기", 40)
    assert candidates and all(REG.covers(f, "지금 시작하기") for f in candidates)


@pytest.mark.parametrize("stroke,text", [(np.ones((2, 3), bool), " \n"), (np.zeros((2, 3), bool), "Text"), (np.zeros((0, 0), bool), "Text")])
def test_empty_text_or_stroke_skips_matching(monkeypatch, stroke, text):
    monkeypatch.setattr(fontmatch, "match_font", lambda *a, **k: pytest.fail("empty inputs must not match"))
    assert font_candidates(stroke, text, 40) == []


@pytest.mark.parametrize("text,size", [("x" * 201, 40), ("Text", float("inf")), ("Text", float("-inf")),
                                       ("Text", float("nan")), ("Text", 0), ("Text", -1)])
def test_font_candidate_limits_skip_work(monkeypatch, text, size):
    monkeypatch.setattr(fontmatch, "match_font", lambda *a, **k: pytest.fail("invalid inputs must not match"))
    assert font_candidates(np.ones((2, 3), bool), text, size) == []


@pytest.mark.parametrize("error", [OSError, ValueError, MemoryError, LookupError])
def test_matcher_failure_gives_no_candidates(monkeypatch, error):
    def boom(*a, **k):
        raise error("font unavailable")
    monkeypatch.setattr(fontmatch, "match_font", boom)
    assert font_candidates(np.ones((20, 60), bool), "Text", 40) == []
    assert font_family_guess(np.ones((20, 60), bool), "Text", 40, []) == "sans-serif"


def test_font_candidates_survive_serialization_without_shared_defaults():
    first, second = FontGuess(), FontGuess()
    first.candidates.append("DejaVu Sans")
    assert second.candidates == []
    assert FontGuess.model_validate_json(first.model_dump_json()).candidates == ["DejaVu Sans"]


def test_text_props_leaves_the_font_to_the_style_phase(monkeypatch):
    frames = np.zeros((1, 30, 50, 3), np.uint8)
    frames[0, 8:12, 10:24] = 255
    track = TextTrack(id=1, boxes={0: TextBox(0, "Text", (5, 3, 35, 23), 0.9)}, text="Text")
    monkeypatch.setattr(fontmatch, "match_font", lambda *a, **k: pytest.fail("text_props must not match fonts"))
    raw, canon, cf, font, color = text_props(track, frames, (0, 0, 0), 1, 0)
    assert (font.family_guess, font.candidates, font.confidence) == ("sans-serif", [], 0.5)
    assert (font.size_px, font.weight, cf, color) == (16.0, 400, 0, "#ffffff")
    assert canon.shape == (20, 30, 4)
    np.testing.assert_array_equal(raw[0], [20, 13, 1, 1, 0, 0, 0, 1, 1])


@pytest.mark.parametrize("error", [OSError, ValueError, MemoryError, RuntimeError])
def test_font_match_failure_does_not_abort_sprites_stage(tmp_path, monkeypatch, error):
    from keepframe.analyze.pipeline import AnalyzeOptions, _stage_sprites

    frames = np.zeros((1, 30, 50, 3), np.uint8)
    frames[0, 8:12, 10:24] = 255
    track = TextTrack(id=1, boxes={0: TextBox(0, "Text", (5, 3, 35, 23), 0.9)}, text="Text")

    def boom(*args, **kw):
        raise error("font unavailable")

    monkeypatch.setattr(fontmatch, "match_font", boom)
    (tmp_path / "stages").mkdir()
    props = _stage_sprites(frames, (0, 0, 0), [track], [], [], AnalyzeOptions(refine=False), tmp_path, 1)
    assert props["t1"]["font"].family_guess == "sans-serif"
    assert props["t1"]["font"].candidates == [] and props["t1"]["font"].confidence <= 0.5
    assert props["t1"]["font_error"].startswith(error.__name__)
    pad = props["t1"]["texture_meta"]["padding"]   # textures v2 pad the texture by pad px a side
    assert props["t1"]["canon"].shape == (20 + 2 * pad, 30 + 2 * pad, 4)
    assert (tmp_path / "stages/props.pkl").is_file()


def test_old_matcher_names_are_gone():
    assert not hasattr(fonts, "installed_families") and not hasattr(fonts, "FONT_FAMILIES")
