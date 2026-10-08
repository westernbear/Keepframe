"""Styled text raster: the numpy side of the composer's text CSS (`fonts/css.py`).

Both sides follow Chromium with `text-rendering: geometricPrecision` (unhinted outlines, fractional advances):
- line box = box height / lines, in LayoutUnits (1/64 px); ascent/descent from OS/2 typo metrics when
  USE_TYPO_METRICS is set, else hhea, each rounded, a pixel borrowed from the ascent when the descent rounded
  down; first baseline = ascent + floor(half leading); baselines snap to whole pixels;
- a run is shaped whole (Pillow/raqm = HarfBuzz); with tracking each character sits at its prefix advance plus
  the pair kerning before it plus i·t, ligatures off (Chromium drops liga/clig under letter-spacing), and the
  spacing also follows the last character;
- runs split by cmap between the primary face and the fallback, the fallback at size × fallback_scale
  (`@font-face size-adjust`);
- glyphs are drawn at 8× and area-averaged (FreeType would hint at 1×; Chromium does not here), sheared about the
  first baseline (`skewX(−θ)`);
- effects: shadow/glow `op·C·GaussianBlur(shift(α, dx, dy), σ = blur/2)`, stroke `op·dilate(α, w)` under the
  fill (the visible ring is dilate(α, w) − α), painted shadows → strokes → fill; a gradient fill is sampled in the
  span's unsheared coordinates (CSS skews the background with the text); the fade multiplies the final alpha.
"""
from __future__ import annotations

import functools
import hashlib
import math
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import cv2
import numpy as np

from ..ir.colour import hex_to_rgb8
from ..ir.gradient import gradient_t, render_gradient
from ..ir.paths import scene_asset_path
from ..ir.schema import Fade, FontGuess, Gradient, GradientStop, TextEffect, TextStyle
from ..log import get
from .registry import FontFace, FontRegistry, _cmap

log = get("keepframe.fonts")
SS = 8                       # supersampling factor for glyph rendering
MAX_SS_PIXELS = 48_000_000   # supersampled canvas budget; SS halves until the canvas fits
LIGA_OFF = ("-liga", "-clig")
FONT_SUFFIXES = (".ttf", ".otf", ".woff", ".woff2")
_HEX = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})")
_HANGUL = re.compile(r"[\uac00-\ud7a3\u1100-\u11ff\u3130-\u318f]")
_FT_LOCK = threading.RLock()   # cached FreeType faces are shared; FreeType faces are not thread-safe
# Scene JSON is untrusted: every size that reaches an allocation is bounded (clamped and logged), the same way
# on both sides so sane values keep parity.
MAX_TEXT_PX = 2048.0           # font size, and at most 8x the line box
MAX_BOX_SIDE = 16384           # texture side
MAX_CANVAS_PX = 8_000_000      # 1x canvas: box + effect padding
MAX_PAD = 512
MAX_GLYPH_MASK = 16_000_000    # one Pillow draw call (its mask spans the whole string), supersampled
MAX_CHARS = 4000               # characters laid out per element
MAX_STROKE_PX, MAX_BLUR_PX, MAX_OFFSET_PX = 32.0, 128.0, 256.0   # also <= 1/4, 1, 1 x the box side
TRACKING_EM = (-0.5, 2.0)
MAX_SHEAR_DEG = 60.0
FALLBACK_SCALE = (0.25, 4.0)
# Styled text in a generic or uninstalled family draws a bundled face on both sides (R34).
DEFAULT_FAMILY = "Inter"
GENERIC_FAMILIES = {"serif": "Source Serif 4", "ui-serif": "Source Serif 4", "monospace": "JetBrains Mono",
                    "ui-monospace": "JetBrains Mono", "cursive": "Dancing Script"}


@dataclass(frozen=True)
class TextFonts:
    """The faces one text element draws with, as both the raster and the CSS resolve them."""
    primary: FontFace
    fallback: FontFace | None = None
    weight: int = 400            # primary weight clamped into its face (CSS font-weight)
    fallback_weight: int = 400
    fallback_scale: float = 1.0

    @property
    def embedded(self) -> bool:
        return self.primary.source in ("bundled", "uploaded")


def _finite(v: float, default: float = 0.0) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _clamp(v: float, lo: float, hi: float, default: float = 0.0) -> float:
    return min(max(_finite(v, default), lo), hi)


def lu(v: float) -> float:
    """A CSS length as Blink lays it out: 4 decimals in the page (non-finite = 0, as the CSS writes it), then
    LayoutUnits."""
    return round(round(_finite(v), 4) * 64) / 64


def hex_rgb(value: str | None, default: str = "#000000") -> tuple[int, int, int]:
    return hex_to_rgb8(value if isinstance(value, str) and _HEX.fullmatch(value) else default)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


@functools.lru_cache(maxsize=128)
def _info(path: str, index: int, mtime: float) -> dict:
    from fontTools.ttLib import TTFont
    with TTFont(path, fontNumber=index, lazy=True) as f:
        upm = f["head"].unitsPerEm
        hh = f["hhea"]
        os2 = f["OS/2"] if "OS/2" in f else None
        if os2 is not None and os2.fsSelection & (1 << 7):          # USE_TYPO_METRICS (Skia honours it)
            asc, desc = os2.sTypoAscender, -os2.sTypoDescender
        elif hh.ascent or hh.descent or os2 is None:
            asc, desc = hh.ascent, -hh.descent
        else:
            asc, desc = os2.sTypoAscender, -os2.sTypoDescender
        axes = tuple((a.axisTag, float(a.minValue), float(a.defaultValue), float(a.maxValue)) for a in f["fvar"].axes) \
            if "fvar" in f else ()
        weight = int(os2.usWeightClass) if os2 is not None else 400
    return {"upm": upm, "asc": asc, "desc": desc, "axes": axes, "weight": weight}


def font_info(face: FontFace) -> dict:
    return _info(str(face.path), face.index, _mtime(face.path))


def cmap(face: FontFace) -> frozenset[int]:
    try:
        return _cmap(str(face.path), face.index, _mtime(face.path))
    except Exception as e:   # unreadable font: nothing is covered
        log.warning("cmap read failed for %s: %s", face.path.name, e)
        return frozenset()


def clamp_weight(face: FontFace, weight: int | float | None) -> int:
    lo, hi = face.weight_range
    w = min(max(int(round(weight or 400)), 100), 900)
    return int(min(max(w, lo, 1), hi, 1000))


def line_metrics(face: FontFace, size_px: float) -> tuple[int, int]:
    """Ascent and descent in whole pixels as Blink rounds them for subpixel-positioned text."""
    info = font_info(face)
    a, d = info["asc"] * size_px / info["upm"], info["desc"] * size_px / info["upm"]
    ra, rd = math.floor(a + 0.5), math.floor(d + 0.5)
    if rd < d and ra >= 1:   # Blink borrows a pixel from the ascent so descenders are not cut
        ra, rd = ra - 1, rd + 1
    return ra, rd


def first_baseline(face: FontFace, size_px: float, line_h: float) -> int:
    """Baseline of the first line inside a line box of `line_h` px (Blink: ascent + floor(half leading))."""
    ra, rd = line_metrics(face, size_px)
    half = int((round(lu(line_h) * 64) - (ra + rd) * 64) / 2)   # LayoutUnit division truncates
    return ra + math.floor(half / 64)


def baselines(face: FontFace, size_px: float, line_h: float, n: int, dy: float = 0.0) -> list[int]:
    b0 = first_baseline(face, size_px, line_h)
    return [math.floor(lu(dy) + b0 + k * lu(line_h) + 0.5) for k in range(n)]


# --- font resolution -----------------------------------------------------------------------------------------

@functools.lru_cache(maxsize=64)
def _file_face(path: str, mtime: float, family: str, postscript: str | None) -> FontFace:
    info = _info(path, 0, mtime)
    wght = next((a for a in info["axes"] if a[0] == "wght"), None)
    rng = (round(wght[1]), round(wght[3])) if wght else (info["weight"], info["weight"])
    sha = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    cps = _cmap(path, 0, mtime)
    return FontFace(family, "uploaded", Path(path), 0, rng, False, "neo_grotesque", postscript, sha,
                    0x41 in cps, 0xAC00 in cps)


def scene_font_file(font: FontGuess, scene_dir: Path | None) -> Path | None:
    """`FontGuess.file` (a scene asset, pinned by render plans) when it is a font inside the scene directory."""
    if not font.file or scene_dir is None:
        return None
    try:
        path = scene_asset_path(Path(scene_dir), font.file)
    except ValueError:
        log.warning("font file outside the scene ignored")
        return None
    return path if path.suffix.lower() in FONT_SUFFIXES and path.is_file() else None


def embedded_face(font: FontGuess, registry: FontRegistry, scene_dir: Path | None = None) -> bool:
    """True when the primary face is a bundled or uploaded file (drawn from an embedded @font-face)."""
    if scene_font_file(font, scene_dir) is not None:
        return True
    return font.family_guess.casefold() in {f.casefold() for f in registry.families()}


def _needs_glyph(ch: str) -> bool:
    return not ch.isspace() and ord(ch) >= 32


def resolve_fonts(font: FontGuess, text: str = "", registry: FontRegistry | None = None,
                  scene_dir: Path | None = None) -> TextFonts | None:
    """Primary face (scene font file > uploaded > bundled > exact system family > a bundled stand-in for generic
    and uninstalled names), weight, and the fallback for what it lacks."""
    registry = registry or FontRegistry()
    primary = None
    path = scene_font_file(font, scene_dir)
    if path is not None:
        try:
            primary = _file_face(str(path), _mtime(path), font.family_guess, font.postscript)
        except Exception as e:   # unreadable upload: fall back to the registry
            log.warning("scene font %s unreadable: %s", path.name, e)
    weight = min(max(int(_finite(font.weight, 400)), 100), 900)
    if primary is None:
        primary = registry.face(font.family_guess, weight)
    if primary is None:   # Chromium and fontconfig would each pick their own font for these names
        primary = registry.face(GENERIC_FAMILIES.get(font.family_guess.casefold(), DEFAULT_FAMILY), weight)
    if primary is None:
        return None
    fallback, fb_weight, scale = None, font.fallback_weight or weight, 1.0
    if font.fallback:
        fallback = registry.face(font.fallback, min(max(int(_finite(fb_weight, 400)), 100), 900))
        scale = _clamp(font.fallback_scale, *FALLBACK_SCALE, default=1.0)
    if fallback is None:
        covered = cmap(primary)
        if any(_HANGUL.match(ch) for ch in text if _needs_glyph(ch) and ord(ch) not in covered):
            family, fb_weight = registry.hangul_fallback(primary.family, weight)
            fallback, scale = registry.face(family, fb_weight), 1.0
    if fallback is not None and (fallback.path, fallback.index) == (primary.path, primary.index):
        fallback = None
    return TextFonts(primary, fallback, clamp_weight(primary, font.weight),
                     clamp_weight(fallback, fb_weight) if fallback else clamp_weight(primary, font.weight),
                     scale if fallback else 1.0)


def split_runs(line: str, fonts: TextFonts) -> list[tuple[str, bool]]:
    """(text, uses_fallback) runs: a character goes to the fallback only when the primary lacks it and it has it."""
    if fonts.fallback is None:
        return [(line, False)] if line else []
    pc, fc = cmap(fonts.primary), cmap(fonts.fallback)
    runs: list[tuple[str, bool]] = []
    for ch in line:
        fb = ord(ch) not in pc and ord(ch) in fc
        if runs and runs[-1][1] == fb:
            runs[-1] = (runs[-1][0] + ch, fb)
        else:
            runs.append((ch, fb))
    return runs


# --- glyph layout and coverage ----------------------------------------------------------------------------------

@functools.lru_cache(maxsize=64)
def _pil_font(path: str, index: int, size: float, weight: int, mtime: float):
    from PIL import ImageFont
    font = ImageFont.truetype(path, size, index=index)
    axes = _info(path, index, mtime)["axes"]
    if axes:
        font.set_variation_by_axes([min(max(float(weight), lo), hi) if tag == "wght" else default
                                    for tag, lo, default, hi in axes])
    return font


def pil_font(face: FontFace, size: float, weight: int):
    return _pil_font(str(face.path), face.index, float(size), int(weight), _mtime(face.path))


@functools.lru_cache(maxsize=1)
def _raqm() -> bool:
    from PIL import features
    return bool(features.check("raqm"))


def _length(font, text: str, feats: tuple[str, ...] | None) -> float:
    if not text:
        return 0.0
    return font.getlength(text, features=list(feats)) if feats and _raqm() else font.getlength(text)


def _layout_line(line: str, fonts: TextFonts, size: float, ss: int, t: float):
    """Draw ops (x, text, font, features, advance) at ss× and the line's advance (spacing after every character)."""
    ops, x = [], 0.0
    for run, fb in split_runs(line, fonts):
        face = fonts.fallback if fb else fonts.primary
        px = size * (fonts.fallback_scale if fb else 1.0)
        font = pil_font(face, px * ss, fonts.fallback_weight if fb else fonts.weight)
        if t == 0:
            adv = lambda s: _length(font, s, None) / ss
            total = adv(run)
            # one draw call per run keeps ligatures; a run whose mask would be huge goes in pieces
            mask = total * px * 1.5 * ss * ss
            step = len(run) if mask <= MAX_GLYPH_MASK else max(1, int(len(run) * MAX_GLYPH_MASK / mask))
            for c in range(0, len(run), step):
                cx = x if c == 0 else x + adv(run[:c]) + adv(run[c - 1:c + 1]) - adv(run[c - 1]) - adv(run[c])
                ops.append((cx, run[c:c + step], font, None, adv(run[c:c + step])))
            x += total
            continue
        adv = lambda s: _length(font, s, LIGA_OFF) / ss
        cx = x
        for j, ch in enumerate(run):
            if j:   # previous advance + pair kerning (= prefix length + kerning of the new pair), in O(n)
                cx += adv(run[j - 1:j + 1]) - adv(ch) + t
            ops.append((cx, ch, font, LIGA_OFF, adv(ch)))
        x = cx + adv(run[-1]) + t
    return ops, x


def text_size(size_px: float, line_h: float | None = None) -> float:
    """Font size as both sides draw it: positive, at most MAX_TEXT_PX and 8x the line box."""
    size = _finite(size_px)
    if size <= 0:
        raise ValueError("text size must be positive")
    cap = MAX_TEXT_PX if line_h is None else min(MAX_TEXT_PX, max(8.0, 8.0 * _finite(line_h, MAX_TEXT_PX)))
    if size > cap:
        log.warning("text size %.0f px clamped to %.0f", size, cap)
        size = cap
    return round(size, 4)


def bounded_style(style: TextStyle | None, box: tuple[float, float] | None = None) -> TextStyle:
    """The style with every number finite and bounded (relative to the box side when known)."""
    style = style or TextStyle()
    side = MAX_BOX_SIDE if box is None else max(1.0, min(_finite(max(box), 1.0), MAX_BOX_SIDE))
    off = min(MAX_OFFSET_PX, side)
    effects = [e.model_copy(update={"width": _clamp(e.width, 0.0, min(MAX_STROKE_PX, side / 4)),
                                    "blur": _clamp(e.blur, 0.0, min(MAX_BLUR_PX, side)),
                                    "dx": _clamp(e.dx, -off, off), "dy": _clamp(e.dy, -off, off),
                                    "opacity": _clamp(e.opacity, 0.0, 1.0, 1.0)}) for e in style.effects]
    out = style.model_copy(update={"dx": _clamp(style.dx, -side, side), "dy": _clamp(style.dy, -side, side),
                                   "tracking_em": _clamp(style.tracking_em, *TRACKING_EM),
                                   "shear_deg": _clamp(style.shear_deg, -MAX_SHEAR_DEG, MAX_SHEAR_DEG),
                                   "effects": effects})
    if out != style:
        log.warning("text style values clamped to bounds")
    return out


def _cap_chars(lines: Sequence[str]) -> list[str]:
    out, left = [], MAX_CHARS
    for line in lines:
        out.append(line[:max(0, left)])
        left -= len(line)
    if left < 0:
        log.warning("styled text cut to %d characters", MAX_CHARS)
    return out or [""]


def bounded_canvas(box: tuple[float, float], pad: int) -> tuple[int, int, int]:
    """Texture width, height and effect padding, within MAX_BOX_SIDE / MAX_PAD / MAX_CANVAS_PX."""
    w = int(min(max(1, round(_finite(box[0], 1.0))), MAX_BOX_SIDE))
    h = int(min(max(1, round(_finite(box[1], 1.0))), MAX_BOX_SIDE))
    pad = int(min(max(0, pad), MAX_PAD))
    if (w + 2 * pad) * (h + 2 * pad) > MAX_CANVAS_PX:
        log.warning("text canvas %dx%d (+%d) bounded", w, h, pad)
        while pad and (w + 2 * pad) * (h + 2 * pad) > MAX_CANVAS_PX:
            pad //= 2
        if w * h > MAX_CANVAS_PX:
            f = math.sqrt(MAX_CANVAS_PX / (w * h))
            w, h = max(1, int(w * f)), max(1, int(h * f))
    return w, h, pad


def _shear_overhang(k: float, ra: int, rd: int, n: int, line_h: float) -> float:
    return k * ra if k > 0 else -k * ((n - 1) * line_h + rd)


def natural_box(lines: Sequence[str], size_px: float, fonts: TextFonts, *, tracking_em: float = 0.0,
                shear_deg: float = 0.0, dx: float = 0.0) -> tuple[int, int]:
    """Smallest box the text lays out in: one ascent+descent per line, the widest advance plus the shear."""
    lines = _cap_chars(lines)
    size = text_size(size_px)
    ra, rd = line_metrics(fonts.primary, size)
    n = len(lines)
    t = size * _clamp(tracking_em, *TRACKING_EM)
    k = math.tan(math.radians(_clamp(shear_deg, -MAX_SHEAR_DEG, MAX_SHEAR_DEG)))
    with _FT_LOCK:
        ext = max((_layout_line(line, fonts, size, SS, t)[1] for line in lines), default=0.0)
    w = lu(_clamp(dx, -MAX_BOX_SIDE, MAX_BOX_SIDE)) + ext + max(0.0, _shear_overhang(k, ra, rd, n, ra + rd))
    return int(min(max(1, math.ceil(w - 1e-6)), MAX_BOX_SIDE)), int(min(max(1, n * (ra + rd)), MAX_BOX_SIDE))


def _render_alpha(lines: Sequence[str], size_px: float, fonts: TextFonts, *, tracking_em: float, shear_deg: float,
                  box: tuple[float, float] | None, dx: float, dy: float, pad: int) -> np.ndarray:
    from PIL import Image, ImageDraw
    lines = _cap_chars(list(lines) or [""])
    tracking_em = _clamp(tracking_em, *TRACKING_EM)
    shear_deg = _clamp(shear_deg, -MAX_SHEAR_DEG, MAX_SHEAR_DEG)
    dx, dy = _clamp(dx, -MAX_BOX_SIDE, MAX_BOX_SIDE), _clamp(dy, -MAX_BOX_SIDE, MAX_BOX_SIDE)
    if box is None:
        box = natural_box(lines, size_px, fonts, tracking_em=tracking_em, shear_deg=shear_deg, dx=dx)
        line_h = float(box[1]) / len(lines)
        size = text_size(size_px)
    else:
        box = (_clamp(box[0], 1.0, 1e9, 1.0), _clamp(box[1], 1.0, 1e9, 1.0))
        line_h = round(float(box[1]) / len(lines), 4)
        size = text_size(size_px, line_h)
    W, H, pad = bounded_canvas(box, pad)
    cw, ch = W + 2 * pad, H + 2 * pad
    k = math.tan(math.radians(shear_deg))
    y0 = pad + lu(dy) + first_baseline(fonts.primary, size, line_h)   # skewX(−θ) about the first baseline
    # glyphs the shear brings into the canvas are drawn on a side margin first
    margin = int(min(math.ceil(abs(k) * max(abs(y0), abs(ch - y0))), 2 * cw)) if k else 0
    glyph_px = size * max(1.0, fonts.fallback_scale if fonts.fallback else 1.0) * 1.5
    ss = SS
    while ss > 1 and ((cw + 2 * margin) * ch * ss * ss > MAX_SS_PIXELS or (glyph_px * ss) ** 2 > MAX_GLYPH_MASK):
        ss //= 2
    t = size * tracking_em
    img = Image.new("L", ((cw + 2 * margin) * ss, ch * ss), 0)
    draw = ImageDraw.Draw(img)
    x0 = margin + pad + lu(dx)
    ys = baselines(fonts.primary, size, line_h, len(lines), dy)
    with _FT_LOCK:
        for line, y in zip(lines, ys):
            if not -glyph_px < pad + y < ch + glyph_px:
                continue
            for x, text, font, feats, adv in _layout_line(line, fonts, size, ss, t)[0]:
                if x0 + x + adv < -glyph_px or x0 + x > cw + 2 * margin + glyph_px:
                    continue
                kw = {"features": list(feats)} if feats and _raqm() else {}
                draw.text(((x0 + x) * ss, (pad + y) * ss), text, font=font, fill=255, anchor="ls", **kw)
    a = np.asarray(img, np.float32) / 255.0
    if k:
        m = np.float32([[1, -k, k * (y0 * ss - 0.5)], [0, 1, 0]])
        a = cv2.warpAffine(a, m, (a.shape[1], a.shape[0]), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    a = a[:, margin * ss:(margin + cw) * ss]
    if ss > 1:
        a = cv2.resize(a, (cw, ch), interpolation=cv2.INTER_AREA)
    return np.clip(a, 0.0, 1.0).astype(np.float32)


def glyph_alpha(lines: Sequence[str], size_px: float, faces: Sequence[FontFace], *, weight: int, tracking_em: float = 0.0,
                shear_deg: float = 0.0, box: tuple[float, float] | None = None, dx: float = 0.0, dy: float = 0.0,
                fallback_scale: float = 1.0, fallback_weight: int | None = None, pad: int = 0) -> np.ndarray:
    """Coverage (H×W float32, plus `pad` px on every side) of `lines` laid out in `box` as the composer CSS lays
    them out; box None = the natural box. faces = [primary, fallback?]."""
    primary, fallback = faces[0], (faces[1] if len(faces) > 1 else None)
    fonts = TextFonts(primary, fallback, clamp_weight(primary, weight),
                      clamp_weight(fallback, fallback_weight or weight) if fallback else clamp_weight(primary, weight),
                      _clamp(fallback_scale, *FALLBACK_SCALE, default=1.0) if fallback else 1.0)
    return _render_alpha(lines, size_px, fonts, tracking_em=tracking_em, shear_deg=shear_deg, box=box, dx=dx, dy=dy, pad=pad)


# --- paint ----------------------------------------------------------------------------------------------------

def _shift(a: np.ndarray, dx: float, dy: float) -> np.ndarray:
    if not dx and not dy:
        return a
    return cv2.warpAffine(a, np.float32([[1, 0, dx], [0, 1, dy]]), (a.shape[1], a.shape[0]), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=0)


def _blur(a: np.ndarray, sigma: float) -> np.ndarray:
    return cv2.GaussianBlur(a, (0, 0), sigma, borderType=cv2.BORDER_CONSTANT) if sigma > 0 else a


def _dilate(a: np.ndarray, w: float) -> np.ndarray:
    def disk(r: int) -> np.ndarray:
        if r <= 0:
            return a
        return cv2.dilate(a, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1)),
                          borderType=cv2.BORDER_CONSTANT, borderValue=0)
    lo = math.floor(w)
    f = w - lo
    d = disk(lo)
    return d if f < 1e-6 else (1 - f) * d + f * disk(lo + 1)


def compose_text(alpha: np.ndarray, fill, effects: Iterable[TextEffect]) -> np.ndarray:
    """Straight RGBA float32 (0..1): shadows/glows, then strokes, then the fill (colour or H×W×3 array) over them."""
    alpha = np.asarray(alpha, np.float32)
    h, w = alpha.shape
    fill_rgb = np.broadcast_to(np.asarray(fill, np.float32), (h, w, 3))
    acc_c = np.zeros((h, w, 3), np.float32)
    acc_a = np.zeros((h, w), np.float32)

    def over(rgb, a):
        nonlocal acc_c, acc_a
        a = np.clip(a, 0.0, 1.0).astype(np.float32)
        acc_c = np.asarray(rgb, np.float32) * a[..., None] + acc_c * (1 - a[..., None])
        acc_a = a + acc_a * (1 - a)

    effects = bounded_style(TextStyle(effects=list(effects)[:4]), (w, h)).effects
    for e in effects:
        if e.kind in ("shadow", "glow"):
            a = _blur(_shift(alpha, _finite(e.dx), _finite(e.dy)), max(0.0, _finite(e.blur)) / 2)
            over(np.array(hex_rgb(e.color), np.float32) / 255, min(max(_finite(e.opacity, 1.0), 0.0), 1.0) * a)
    for e in effects:
        if e.kind == "stroke" and _finite(e.width) > 0:
            over(np.array(hex_rgb(e.color), np.float32) / 255, min(max(_finite(e.opacity, 1.0), 0.0), 1.0) * _dilate(alpha, e.width))
    over(fill_rgb, alpha)
    out = np.empty((h, w, 4), np.float32)
    nz = acc_a > 1e-6
    out[..., :3] = fill_rgb
    out[nz, :3] = np.clip(acc_c[nz] / acc_a[nz, None], 0.0, 1.0)
    out[..., 3] = acc_a
    return out


def effect_pad(effects: Iterable[TextEffect]) -> int:
    pad = 0.0
    for e in effects:
        if e.kind == "stroke":
            pad = max(pad, _finite(e.width) + 1)
        else:
            reach = 2 * max(0.0, _finite(e.blur)) + 1        # 4σ
            pad = max(pad, abs(_finite(e.dx)) + reach, abs(_finite(e.dy)) + reach)
    return int(math.ceil(min(pad, MAX_PAD)))


def fade_stops(fade: Fade) -> tuple[float, list[tuple[float, float]]]:
    """Angle and sorted (offset, alpha) stops, clamped to 0..1, shared by the raster and the CSS mask."""
    stops = sorted((min(max(_finite(s.offset), 0.0), 1.0), min(max(_finite(s.alpha, 1.0), 0.0), 1.0)) for s in fade.stops)
    return _finite(fade.angle, 180.0), stops


def _grid(w: int, h: int, pad: int = 0) -> tuple[np.ndarray, np.ndarray]:
    ys, xs = np.mgrid[0:h + 2 * pad, 0:w + 2 * pad].astype(np.float64)
    return xs - pad + 0.5, ys - pad + 0.5


def fade_alpha(fade: Fade, w: int, h: int, box: tuple[float, float] | None = None) -> np.ndarray:
    """The fade mask over w×h pixels of a (possibly fractional) box, as the wrapper's CSS mask draws it."""
    angle, stops = fade_stops(fade)
    g = Gradient(kind="linear", angle=angle, stops=[GradientStop(offset=0, color="#000000"), GradientStop(offset=1, color="#000000")])
    bw, bh = box or (w, h)
    t = np.clip(gradient_t(g, float(bw), float(bh), _grid(w, h)), 0.0, 1.0)
    return np.interp(t, [o for o, _ in stops], [a for _, a in stops]).astype(np.float32)


def gradient_fill(g: Gradient, box: tuple[float, float], w: int, h: int, pad: int, *, shear_deg: float = 0.0,
                  y0: float = 0.0) -> np.ndarray:
    """The fill gradient (geometry of the box) over w×h pixels plus `pad`, at the coordinates CSS samples its
    background: box coordinates before the span's skewX(−θ) about y0."""
    x, y = _grid(w, h, pad)
    k = math.tan(math.radians(shear_deg))
    if k:
        x = x + k * (y - y0)
    return render_gradient(g, float(box[0]), float(box[1]), (x, y)).astype(np.float32) / 255.0


def render_styled(lines: Sequence[str], font: FontGuess, color: str | None, style: TextStyle | None, *,
                  registry: FontRegistry | None = None, scene_dir: Path | None = None,
                  box: tuple[float, float] | None = None) -> np.ndarray:
    """Un-premultiplied RGBA uint8 texture of the styled text, box-sized (natural box when None)."""
    lines = _cap_chars(list(lines) or [""])
    style = bounded_style(style)
    fonts = resolve_fonts(font, "\n".join(lines), registry, scene_dir)
    if fonts is None:
        raise LookupError(f"no font file for {font.family_guess!r}")
    if box is None:
        box = natural_box(lines, text_size(font.size_px), fonts, tracking_em=style.tracking_em,
                          shear_deg=style.shear_deg, dx=style.dx)
    box = (_clamp(box[0], 1.0, 1e9, 1.0), _clamp(box[1], 1.0, 1e9, 1.0))
    style = bounded_style(style, box)
    line_h = round(float(box[1]) / len(lines), 4)
    size = text_size(font.size_px, line_h)
    w, h, pad = bounded_canvas(box, effect_pad(style.effects))
    alpha = _render_alpha(lines, size, fonts, tracking_em=style.tracking_em, shear_deg=style.shear_deg, box=box,
                          dx=style.dx, dy=style.dy, pad=pad)
    if style.fill is not None:
        y0 = lu(style.dy) + first_baseline(fonts.primary, size, line_h)
        fill = gradient_fill(style.fill, box, w, h, pad, shear_deg=style.shear_deg, y0=y0)
    else:
        fill = np.array(hex_rgb(color), np.float32) / 255
    rgba = compose_text(alpha, fill, style.effects)[pad:pad + h, pad:pad + w]
    if style.fade is not None:
        rgba[..., 3] *= fade_alpha(style.fade, w, h, box)
    return np.round(np.clip(rgba, 0.0, 1.0) * 255).astype(np.uint8)
