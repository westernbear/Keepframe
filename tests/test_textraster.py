from pathlib import Path

import cv2
import numpy as np
import pytest

from keepframe.edit import apply as edit_apply, textraster
from keepframe.edit.apply import apply_edit, measure_text, write_text_texture
from keepframe.edit.intent import Intent, Target, plan
from keepframe.edit.textraster import font_path, measure, render_lines, resolve_family
from keepframe.ir.schema import Background, Canonical, Element, FontGuess, Scene

needs_font = pytest.mark.skipif(font_path() is None, reason="no fontconfig font with Hangul")


@needs_font
def test_hangul_is_measured_as_full_width_glyphs():
    w, _ = measure_text("가", 32)          # Hershey drew '?' per UTF-8 byte (~70px)
    assert 20 <= w - 4 <= 40


def test_docker_image_has_chromium_and_cjk_fonts():
    text = Path("Dockerfile").read_text()
    assert "fonts-noto-cjk" in text and "playwright install --with-deps chromium" in text
    assert text.count("fontconfig") == 2 and text.count("fonts-noto-cjk") == 2


@needs_font
def test_resolves_actual_family():
    assert Path(font_path()).is_file()
    assert resolve_family("sans-serif")


@needs_font
def test_render_lines_has_bgra_color_alpha_and_measured_dimensions():
    lines = ["가나다", "Hello gy"]
    sizes = [measure(line, 32) for line in lines]
    img = render_lines(lines, 32, (17, 101, 231))
    assert img.dtype == np.uint8
    assert img.shape == (sum(h for _, h in sizes), max(w for w, _ in sizes), 4)
    assert img[:sizes[0][1], :, 3].any() and img[sizes[0][1]:, :, 3].any()
    assert np.all(img[img[..., 3] > 0, :3] == (231, 101, 17))
    assert not img[0, 0].any()


@needs_font
@pytest.mark.parametrize("text,lines", [("가", None), ("unused", ["가나다", "Hello"]), ("", None)])
def test_write_texture_creates_png_with_measured_dimensions(tmp_path, text, lines):
    path = tmp_path / "assets" / "text.png"
    w, h = write_text_texture(path, text, 32, (17, 101, 231), lines=lines, family="sans-serif")
    rows = lines or [text]
    sizes = [measure_text(row, 32, "sans-serif") for row in rows]
    assert (w, h) == (max(w for w, _ in sizes), sum(h for _, h in sizes))
    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    assert img.shape == (h, w, 4)
    assert bool(img[..., 3].any()) == bool(text or lines)


@pytest.mark.parametrize("lines", [None, ["Hello", "world"]])
def test_no_fontconfig_retains_hershey_fallback(tmp_path, monkeypatch, lines):
    font_path.cache_clear()
    resolve_family.cache_clear()
    monkeypatch.setattr(textraster.shutil, "which", lambda _: None)
    try:
        assert font_path() is None and resolve_family("sans-serif") is None
        assert measure("Hello", 22) is None
        assert render_lines(["Hello"], 22, (17, 101, 231)) is None
        (tw, th), base = cv2.getTextSize("Hello", cv2.FONT_HERSHEY_SIMPLEX, 1.0, 2)
        assert measure_text("Hello", 22) == (tw + 4, th + base + 4)
        path = tmp_path / "assets" / "text.png"
        w, h = write_text_texture(path, "Hello", 22, (17, 101, 231), lines=lines)
        img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        assert img.shape == (h, w, 4) and img[..., 3].any()
        assert (img[..., 3] == 255).any()
        assert np.all(img[img[..., 3] == 255, :3] == (231, 101, 17))
    finally:
        font_path.cache_clear()
        resolve_family.cache_clear()


def _scene():
    return Scene(id="s1", size=(200, 100), fps=30, frames=10, background=Background(), elements=[
        Element(id="e1", kind="text", visible=(0, 9), canonical=Canonical(
            width=40, height=20, text="Old", color="#ffffff", font=FontGuess(family_guess="Chosen font", size_px=20),
        )),
    ])


def test_plan_uses_element_font_family(monkeypatch):
    monkeypatch.setattr(edit_apply, "measure_text", lambda text, size, family="sans-serif": (60 if family == "Chosen font" else 30, 20))
    intent = Intent(targets=[Target(element="e1", property="text", value="New")])
    assert plan(_scene(), intent).conflicts[0].id == "overflow"


@pytest.mark.parametrize("prop,overflow", [("text", None), ("text", "wrap"), ("text", "shrink_font"), ("color", None)])
def test_edits_forward_font_family_to_measurement_and_raster(tmp_path, monkeypatch, prop, overflow):
    measured, rendered = [], []

    def fake_measure(text, size, family="sans-serif"):
        measured.append(family)
        return (60 if size > 18 else 30, 20)

    def fake_render(lines, size, rgb, family="sans-serif"):
        rendered.append((family, size))
        return np.zeros((20, 30, 4), np.uint8)

    monkeypatch.setattr(edit_apply, "_measure", fake_measure)
    monkeypatch.setattr(edit_apply, "render_lines", fake_render)
    target = Target(element="e1", property=prop, value="New words" if prop == "text" else "#112233")
    result = apply_edit(_scene(), tmp_path, [target], {"overflow": overflow} if overflow else {}, None)
    assert rendered == [("Chosen font", 18 if overflow == "shrink_font" else 20)]
    if overflow == "shrink_font":
        assert measured and set(measured) == {"Chosen font"}
    assert (tmp_path / result.element("e1").canonical.texture).is_file()
