import numpy as np
import pytest
from keepframe.analyze import fonts
from keepframe.analyze.fonts import FONT_FAMILIES, font_candidates, installed_families
from keepframe.analyze.text import TextBox, TextTrack, text_props
from keepframe.edit.textraster import font_path, render_lines, resolve_families
from keepframe.ir.schema import FontGuess


@pytest.mark.skipif(len(installed_families()) < 2, reason="needs two installed candidate fonts")
@pytest.mark.parametrize("size", [40, 300])
def test_rendered_family_ranks_first(size):
    family = installed_families()[0]
    alpha = render_lines(["Launch faster"], size, (255, 255, 255), family)[..., 3] > 127
    ys, xs = np.nonzero(alpha)
    stroke = alpha[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    candidates = font_candidates(stroke, "Launch faster", size)
    assert candidates[0] == family
    assert len(candidates) == min(3, len(installed_families()))
    assert len(set(candidates)) == len(candidates)
    assert font_candidates(stroke, "Launch faster", size, k=1) == [family]


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
    def guess(stroke, text, size, ranked):
        assert ranked == candidates
        calls.append((stroke.copy(), text, size))
        return "sans-serif"
    monkeypatch.setattr("keepframe.analyze.text.font_family_guess", guess)
    raw, canon, cf, font, color = text_props(track, frames, (0, 0, 0), 1, 0)
    stroke, text, size = calls[0]
    assert len(calls) == 2 and stroke.shape == (4, 14) and stroke.all()
    np.testing.assert_array_equal(calls[1][0], stroke)
    assert calls[1][1:] == (text, size)
    assert (text, size) == ("Text", 16.0)
    assert font.candidates == candidates
    assert font.family_guess == "sans-serif"
    assert (font.size_px, font.weight, cf, color) == (16.0, 400, 0, "#ffffff")
    assert canon.shape == (20, 30, 4)
    np.testing.assert_array_equal(raw[0], [20, 13, 1, 1, 0, 0, 0, 1, 1])


def test_hangul_candidates_use_own_faces_and_distinct_renders():
    text = "지금 시작하기"
    renders = {}
    for family in installed_families():
        path = font_path(family, hangul=False)
        if path and font_path(family, hangul=True) == path:
            img = render_lines([text], 40, (255, 255, 255), family)
            if img is not None and (img[..., 3] > 127).any():
                renders[family] = (img.shape, img.tobytes())
    candidates = font_candidates(np.ones((30, 180), bool), text, 40, k=len(FONT_FAMILIES))
    assert set(candidates) <= set(renders)
    assert len(candidates) == len(set(renders.values()))
    assert len({renders[family] for family in candidates}) == len(candidates)
    for family in candidates:
        assert family == next(f for f, raster in renders.items() if raster == renders[family])


@pytest.mark.parametrize("text", ["지금 시작하기", "가", "ㄱ"])
def test_hangul_fallback_families_are_not_scored(monkeypatch, text):
    families = FONT_FAMILIES[:3]
    monkeypatch.setattr(fonts, "installed_families", lambda: families)

    def path(family, hangul=False):
        return "own.ttf" if family == families[0] or not hangul else "fallback.ttf"

    monkeypatch.setattr(fonts, "font_path", path, raising=False)
    calls = []

    def render(lines, size, rgb, family):
        calls.append(family)
        return np.full((4, 6, 4), 255, np.uint8)

    monkeypatch.setattr(fonts, "render_lines", render)
    assert font_candidates(np.ones((2, 3), bool), text, 40) == [families[0]]
    assert calls == [families[0]]


def test_equal_scores_follow_font_families_order(monkeypatch):
    families = (FONT_FAMILIES[0], FONT_FAMILIES[1], FONT_FAMILIES[-1])
    monkeypatch.setattr(fonts, "installed_families", lambda: families)
    images = {}
    for i, family in enumerate(families):
        img = np.zeros((5, 4, 4), np.uint8)
        img[i + 1, 1] = 255
        images[family] = img
    monkeypatch.setattr(fonts, "render_lines", lambda lines, size, rgb, family: images[family])
    assert font_candidates(np.ones((1, 1), bool), "Text", 40) == list(families)


def test_identical_renders_keep_first_family_without_padding_candidates(monkeypatch):
    families = FONT_FAMILIES[:3]
    monkeypatch.setattr(fonts, "installed_families", lambda: families)
    first = np.zeros((5, 4, 4), np.uint8)
    first[1, 1] = 255
    other = np.zeros_like(first)
    other[2, 1] = 255
    images = dict(zip(families, [first, first.copy(), other]))
    monkeypatch.setattr(fonts, "render_lines", lambda lines, size, rgb, family: images[family])
    assert font_candidates(np.ones((1, 1), bool), "Text", 40) == [families[0], families[2]]
    images[families[2]] = first.copy()
    assert font_candidates(np.ones((1, 1), bool), "Text", 40) == [families[0]]


def test_latin_candidates_do_not_require_hangul_faces(monkeypatch):
    families = FONT_FAMILIES[:2]
    monkeypatch.setattr(fonts, "installed_families", lambda: families)
    monkeypatch.setattr(fonts, "font_path", lambda *args, **kw: pytest.fail("Latin must not filter Hangul faces"), raising=False)
    images = [np.zeros((2, 2, 4), np.uint8), np.full((2, 2, 4), 255, np.uint8)]
    images[0][0, 0] = 255
    images[0][1, 1] = 255
    monkeypatch.setattr(fonts, "render_lines", lambda lines, size, rgb, family: images[families.index(family)])
    assert font_candidates(np.eye(2, dtype=bool), "Text", 40) == list(families)


@pytest.mark.parametrize("error", [OSError, ValueError, MemoryError])
@pytest.mark.parametrize("operation", ["render", "score"])
def test_family_failure_is_skipped_and_ranking_continues(monkeypatch, error, operation):
    families = FONT_FAMILIES[:2]
    monkeypatch.setattr(fonts, "installed_families", lambda: families)
    calls = []

    def render(lines, size, rgb, family):
        calls.append(family)
        if family == families[0] and operation == "render":
            raise error("font unavailable")
        return np.full((4, 6 if family == families[0] else 7, 4), 255, np.uint8)

    resize = fonts.cv2.resize

    def score(glyph, size, interpolation):
        if glyph.shape[1] == 6 and operation == "score":
            raise error("score unavailable")
        return resize(glyph, size, interpolation=interpolation)

    monkeypatch.setattr(fonts, "render_lines", render)
    monkeypatch.setattr(fonts.cv2, "resize", score)
    assert font_candidates(np.ones((2, 3), bool), "Text", 40) == [families[1]]
    assert calls == list(families)


@pytest.mark.parametrize("text,size", [("x" * 201, 40), ("Text", float("inf")), ("Text", float("-inf")),
                                       ("Text", float("nan")), ("Text", 0), ("Text", -1)])
def test_font_candidate_render_limits_skip_work(monkeypatch, text, size):
    monkeypatch.setattr(fonts, "render_lines", lambda *args: pytest.fail("invalid inputs must not render"))
    assert font_candidates(np.ones((2, 3), bool), text, size) == []


@pytest.mark.parametrize("size,render_size", [(40, 40), (128, 128), (129, 128), (256, 128),
                                            (257, 128), (300, 128), (746, 128)])
def test_font_candidate_render_limits_allow_boundary(monkeypatch, size, render_size):
    family = FONT_FAMILIES[0]
    monkeypatch.setattr(fonts, "installed_families", lambda: (family,))
    calls = []

    def render(lines, size, rgb, family):
        calls.append((lines, size))
        return np.full((4, 6, 4), 255, np.uint8)

    monkeypatch.setattr(fonts, "render_lines", render)
    assert font_candidates(np.ones((2, 3), bool), "x" * 200, size) == [family]
    assert calls == [(["x" * 200], render_size)]


@pytest.mark.parametrize("error", [OSError, ValueError, MemoryError])
def test_font_render_failure_does_not_abort_sprites_stage(tmp_path, monkeypatch, error):
    from keepframe.analyze.pipeline import AnalyzeOptions, _stage_sprites

    frames = np.zeros((1, 30, 50, 3), np.uint8)
    frames[0, 8:12, 10:24] = 255
    track = TextTrack(id=1, boxes={0: TextBox(0, "Text", (5, 3, 35, 23), 0.9)}, text="Text")
    monkeypatch.setattr(fonts, "installed_families", lambda: FONT_FAMILIES[:2])

    def render(*args):
        raise error("font unavailable")

    monkeypatch.setattr(fonts, "render_lines", render)
    (tmp_path / "stages").mkdir()
    props = _stage_sprites(frames, (0, 0, 0), [track], [], [], AnalyzeOptions(refine=False), tmp_path, 1)
    assert props["t1"]["font"].family_guess == "sans-serif"
    assert props["t1"]["font"].candidates == []
    assert props["t1"]["canon"].shape == (20, 30, 4)
    assert (tmp_path / "stages/props.pkl").is_file()
