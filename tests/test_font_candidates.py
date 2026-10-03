import numpy as np
import pytest
from keepframe.analyze import fonts
from keepframe.analyze.fonts import FONT_FAMILIES, font_candidates, installed_families
from keepframe.analyze.text import TextBox, TextTrack, text_props
from keepframe.edit.textraster import render_lines, resolve_families
from keepframe.ir.schema import FontGuess


@pytest.mark.skipif(len(installed_families()) < 2, reason="needs two installed candidate fonts")
def test_rendered_family_ranks_first():
    family = installed_families()[0]
    alpha = render_lines(["Launch faster"], 40, (255, 255, 255), family)[..., 3] > 127
    ys, xs = np.nonzero(alpha)
    stroke = alpha[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    candidates = font_candidates(stroke, "Launch faster", 40)
    assert candidates[0] == family
    assert len(candidates) == min(3, len(installed_families()))
    assert len(set(candidates)) == len(candidates)
    assert font_candidates(stroke, "Launch faster", 40, k=1) == [family]


def test_candidates_never_include_uninstalled_fonts():
    assert set(installed_families()) <= set(FONT_FAMILIES)
    assert all(f.casefold() in {n.casefold() for n in resolve_families(f)} for f in installed_families())


def test_installed_families_accepts_localized_names_and_caches(monkeypatch):
    calls = []

    def resolve(name):
        calls.append(name)
        return ("지역 이름", name.upper()) if name == FONT_FAMILIES[0] else ("Fallback",)

    installed_families.cache_clear()
    monkeypatch.setattr(fonts, "resolve_families", resolve)
    try:
        assert installed_families() == (FONT_FAMILIES[0],)
        assert installed_families() == (FONT_FAMILIES[0],)
        assert calls == list(FONT_FAMILIES)
        assert installed_families.cache_info().maxsize == 1
    finally:
        installed_families.cache_clear()


@pytest.mark.parametrize("stroke,text", [(np.ones((2, 3), bool), " \n"), (np.zeros((2, 3), bool), "Text"), (np.zeros((0, 0), bool), "Text")])
def test_empty_text_or_stroke_skips_rendering(monkeypatch, stroke, text):
    monkeypatch.setattr(fonts, "render_lines", lambda *args: pytest.fail("empty inputs must not render"))
    assert font_candidates(stroke, text, 40) == []


def test_unrenderable_and_empty_glyphs_are_skipped(monkeypatch):
    monkeypatch.setattr(fonts, "installed_families", lambda: ("Missing", "Empty", "Visible"))
    images = {"Missing": None, "Empty": np.zeros((4, 6, 4), np.uint8), "Visible": np.full((4, 6, 4), 255, np.uint8)}
    calls = []

    def render(lines, size, rgb, family):
        calls.append((lines, size, rgb, family))
        return images[family]

    monkeypatch.setattr(fonts, "render_lines", render)
    assert font_candidates(np.ones((2, 3), bool), "Text", 40) == ["Visible"]
    assert calls == [(["Text"], 40, (255, 255, 255), f) for f in images]


def test_font_candidates_survive_serialization_without_shared_defaults():
    first, second = FontGuess(), FontGuess()
    first.candidates.append("DejaVu Sans")
    assert second.candidates == []
    assert FontGuess.model_validate_json(first.model_dump_json()).candidates == ["DejaVu Sans"]


@pytest.mark.parametrize("candidates", [["DejaVu Serif", "DejaVu Sans", "Liberation Sans"], []])
def test_text_props_scores_tight_crop_and_keeps_measured_size(monkeypatch, candidates):
    frames = np.zeros((1, 30, 50, 3), np.uint8)
    frames[0, 8:12, 10:24] = 255
    track = TextTrack(id=1, boxes={0: TextBox(0, "Text", (5, 3, 35, 23), 0.9)}, text="Text")
    calls = []

    def rank(stroke, text, size):
        calls.append((stroke.copy(), text, size))
        return candidates

    monkeypatch.setattr("keepframe.analyze.text.font_candidates", rank)
    raw, canon, cf, font, color = text_props(track, frames, (0, 0, 0), 1, 0)
    stroke, text, size = calls[0]
    assert len(calls) == 1 and stroke.shape == (4, 14) and stroke.all()
    assert (text, size) == ("Text", 16.0)
    assert font.candidates == candidates
    assert font.family_guess == (candidates[0] if candidates else "sans-serif")
    assert (font.size_px, font.weight, cf, color) == (16.0, 400, 0, "#ffffff")
    assert canon.shape == (20, 30, 4)
    np.testing.assert_array_equal(raw[0], [20, 13, 1, 1, 0, 0, 0, 1])
