"""Font matching by render-and-compare (Task 10).

The observed coverage of a text (its texture's alpha) is compared with candidate fonts laid out as the composer lays
text out (`raster`): pen positions = previous advance + pair kerning + i·tracking (ligatures off; untracked text as
whole runs with ligatures), Hangul the primary lacks drawn by the fallback face at its scale, shear about the first
baseline.

- Working scale: the observed tight box is WORK_H px high. A candidate is composed from per-glyph bitmaps rendered at
  R px (cached per face, weight and character), area-resized, then warped (size, shear, and the shift that puts its
  ink centroid on the observed one, refined on the column and row profiles). Score = soft IoU Σmin/Σmax.
- Glyph positions: Task 9's glyph centres; else (missing, or no family fits them) each candidate's own, from the
  observed pieces assigned to its predicted glyph spans. Least squares on them gives tracking — and a Hangul
  fallback's scale, which is linear in the positions too.
- Prefilter over every family covering the text, in two stages: (1) layout metrics — |log ink-width ratio| at the
  matched height, the centres' RMS residual, implausible tracking and the stroke-ratio distance to the family's
  calibrated range (relative, capped: it varies with the text) — keep STAGE1_K; (2) a coarse render at the seed
  weight keeps k = 8. (The brief's width + stroke-ratio prefilter alone kept the true family in 10 of 30 synthetic
  samples: tracking and weight change the width as much as the family does.)
- Fit per family, coordinate descent: weight (golden section on wght, on a WEIGHT_STEP grid, seeded by the
  stroke-ratio calibration; static families try each file), tracking (least squares on the glyph positions, else a
  1-D search −0.06…0.12 em step 0.01), size (cap height, then ±4 %), shear (−12…12° step 4, then ±2°; stored only
  when ≥ 4°). Every kept family gets the first round (past MIN_FITS, those whose coarse render trails the best by
  PRUNE are not fitted); the best ROUND2_K the second, and a failing second round keeps the first's fit.
- Confidence 1.0 when the best score ≥ 0.80 and leads the next by ≥ 0.02, else 0.5.
- Strings over 24 characters are matched on their longest run of whole words (one line).
- Hangul with a best face that lacks it: `registry.hangul_fallback`, then its weight and scale refitted
  (`fit_hangul_fallback`, default scale 0.92).
- A scene (`font_guesses`, the style phase): texts one after another, largest cap height first, a repeated text
  refitted with the first one's family, until SCENE_RENDERS candidate renders. Nothing depends on elapsed time, so
  the output is the same on any host and under any load.
- Per font file on disk (`css.cache_root()/match`, keyed by sha256): the stroke-ratio calibration and what stage 1
  reads (glyph metrics, advances, kerning, coverage), so a new process opens only the families the fit reaches.
"""
from __future__ import annotations

import contextvars
import functools
import hashlib
import json
import math
import os
import re
import tempfile
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from ..log import describe, get
from .raster import _FT_LOCK, _HANGUL, LIGA_OFF, _length, _mtime, cmap, first_baseline, font_info
from .registry import FontFace, FontRegistry

log = get("keepframe.fonts.match")

WORK_H = 64.0                    # observed tight box height at the working scale
R = 160                          # glyph bitmap size (px) of every matcher face
MAX_CHARS = 24                   # longer strings are matched on their longest run of whole words …
MAX_TEXT = 400                   # … and longer texts are not matched at all
K_PREFILTER = 8
TRACK_SEARCH = (-0.06, 0.12)     # 1-D tracking search (em) when there are no glyph centres …
TRACK_STEP = 0.01
TRACK_LS = (-0.1, 0.5)           # … least squares on the centres is clamped to this
TRACK_PLAUSIBLE = (-0.08, 0.15)  # prefilter: tracking a family needs beyond this counts against it
STAGE1_K = 12                    # prefilter: families scored by a coarse render (all of them without centres)
SHORT_TEXT = 6                   # texts with at most this many glyphs keep three times as many for stage 2
CENTRES_RMS_MAX = 0.05           # centres no family fits better than this (RMS, em) are not used
PIECES_MIN = 0.6                 # without centres: pieces per character needed to place glyphs per candidate
FIXED_WEIGHT = 400               # the prefilter's first stage draws variable families at this weight (own face)
STAGE2_STEP = 100                # … its second stage at the seed weight rounded to this
ADV_STEP = 50                    # stage 1 takes advances at the seed weight rounded to this
COARSE_TRACK = (-0.03, 0.0, 0.03)   # stage 2 without centres: tracking offsets tried
RATIO_W, RATIO_TOL = 0.1, 0.2    # prefilter: stroke-ratio term weight and relative slack …
RATIO_CAP = 0.02                 # … and its cap: the measure depends on the text (serif capitals: hairlines)
LIG_T = 0.015                    # tracking this small is also tried as 0: whole runs with ligatures (raster's t = 0)
WEIGHT_SPAN = (150.0, 60.0)      # golden-section bracket (± around the current weight) per round …
WEIGHT_ITERS = (2, 2)            # … and its iterations …
WEIGHT_STEP = 25.0               # … on a grid of this many wght units (shared by texts: glyphs cached per weight)
ROUND2_K = 4                     # the second round of coordinate descent refines this many families
MIN_FITS, PRUNE = 3, 0.15        # families past the first MIN_FITS whose coarse score trails the best by PRUNE: skipped
MATCH_FAILED = "match_failed"    # font_guesses result codes (stage data and report messages carry no exception text)
WORK_CAP = "work_cap"
SCENE_RENDERS = 5000             # a scene's texts are matched (largest cap height first) until this many candidate renders
DUP_FITS = 3                     # a repeated text whose own prefilter puts the first match's family on top fits this many
SHEARS = (-12.0, -8.0, -4.0, 0.0, 4.0, 8.0, 12.0)
SHEAR_FINE = 2.0
SHEAR_MIN = 4.0                  # a smaller shear is stored as 0
SIZE_STEPS = (0.96, 0.98, 1.02, 1.04)
REGISTER = (4, 3)                # candidates are registered on the observation within this many working px (x, y)
CONF_TOP, CONF_MARGIN = 0.80, 0.02
FALLBACK_SCALE = 0.92
FALLBACK_SCALES = (0.78, 1.06)
FALLBACK_SEARCH = (0.86, 0.89, 0.92, 0.95, 0.98, 1.01)   # the fit's first round tries these fallback scales …
COARSE_FB = (0.88, 0.92, 0.96, 1.0)                     # … the prefilter's second stage these
CALIB_TEXT = "Hamburg"
CALIB_VERSION = 1                # the cache files' key carries these, the measuring constants, a hash of the measuring
STORE_VERSION = 1                # code and the library versions (`_cache_parts`)
STORE_MAX = 50_000               # entries of a kind per font kept on disk
WEIGHTS = (100, 900)
GOLDEN = (math.sqrt(5) - 1) / 2
CACHE_BYTES = 64 * 1024 * 1024


@dataclass
class FontFit:
    family: str
    weight: int
    size_px: float
    tracking_em: float
    shear_deg: float
    score: float
    dx: float
    dy: float


# --- faces and glyphs -------------------------------------------------------------------------------------------

@functools.lru_cache(maxsize=256)
def _pil(path: str, index: int, mtime: float):
    from PIL import ImageFont
    return ImageFont.truetype(path, R, index=index)


@functools.lru_cache(maxsize=256)
def _pil_fixed(path: str, index: int, mtime: float, weight: float):
    """A second face held at FIXED_WEIGHT for good: the prefilter draws every family with it, so it never pays
    for an instance change (FreeType re-blends every glyph after one: Noto Sans ~4 ms a glyph)."""
    from PIL import ImageFont
    return ImageFont.truetype(path, R, index=index)


def _fixed_weight(face: FontFace) -> float:
    lo, hi = face.weight_range
    return float(min(max(FIXED_WEIGHT, lo), hi))


def _font(face: FontFace, weight: float):
    """The matcher's own Pillow face at R px with wght set (call under _FT_LOCK)."""
    axes = font_info(face)["axes"]
    if axes and abs(weight - _fixed_weight(face)) < 1e-6:
        font = _pil_fixed(str(face.path), face.index, _mtime(face.path), weight)
    else:
        font = _pil(str(face.path), face.index, _mtime(face.path))
    if axes and getattr(font, "_kf_wght", None) != weight:   # the wght it is set to travels with the face
        font.set_variation_by_axes([min(max(float(weight), lo), hi) if tag == "wght" else default
                                    for tag, lo, default, hi in axes])
        font._kf_wght = weight
    return font


def weight_range(face: FontFace) -> tuple[int, int]:
    """The face's wght range within 100–900 (Pretendard 45–930 → 100–900)."""
    lo, hi = max(face.weight_range[0], WEIGHTS[0]), min(face.weight_range[1], WEIGHTS[1])
    return (lo, max(lo, hi)) if lo <= WEIGHTS[1] else (WEIGHTS[1], WEIGHTS[1])


@dataclass
class _Glyph:
    adv: float                         # advance at R
    box: tuple[float, float, float, float]   # ink box (left, top, right, bottom) from the pen on the baseline
    img: np.ndarray | None = None      # uint8 bitmap, top-left at (ox, oy) from the pen
    ox: int = 0
    oy: int = 0
    cx: float = 0.0                    # ink centroid from the pen (R px)
    cy: float = 0.0
    parts: tuple | None = None         # (top, bottom) from the baseline of its solid (α ≥ 0.5) parts (`_with_parts`)
    ink: bool = False                  # it draws something (img may be None: metrics only)

    def metrics(self) -> "_Glyph":
        return _Glyph(self.adv, self.box, None, self.ox, self.oy, self.cx, self.cy, self.parts, self.ink)


@dataclass
class _GlyphSet:
    glyphs: dict[str, _Glyph]
    kern: dict[str, float]             # pair -> kerning at R


class _LRU:
    def __init__(self, budget: int):
        self.budget, self.used, self.items, self.lock = budget, 0, OrderedDict(), threading.Lock()

    def get(self, key):
        with self.lock:
            v = self.items.get(key)
            if v is not None:
                self.items.move_to_end(key)
            return v

    def put(self, key, value, nbytes: int):
        with self.lock:
            if key in self.items:
                return
            self.items[key] = (value, nbytes)
            self.used += nbytes
            while self.used > self.budget and len(self.items) > 1:
                _, (_, n) = self.items.popitem(last=False)
                self.used -= n


_CACHE = _LRU(CACHE_BYTES)   # glyphs with bitmaps


def _bounded(d: OrderedDict, n: int = 200_000) -> None:
    while len(d) > n:
        d.popitem(last=False)


_METRICS: OrderedDict = OrderedDict()   # the same glyphs without bitmaps: the prefilter's stage 1 needs only these
_KERN: OrderedDict = OrderedDict()
_ADV: OrderedDict = OrderedDict()


# --- the prefilter's per-font data on disk --------------------------------------------------------------------
# Stage 1 reads every eligible family: glyph metrics at its drawing weight, advances on the ADV_STEP grid, pair
# kerning and coverage. They are kept per font file next to the calibration, so a new process draws (and opens) only
# the families the fit reaches. Values are the computed ones, so results never depend on what was cached.

class _FaceStore:
    def __init__(self, path: Path):
        self.path, self.lock, self.dirty = path, threading.Lock(), False
        self.data: dict = {"g": {}, "k": {}, "a": {}, "c": None}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if all(isinstance(raw.get(k), dict) for k in ("g", "k", "a")):
                self.data = {"g": raw["g"], "k": raw["k"], "a": raw["a"], "c": raw.get("c")}
        except (OSError, ValueError, AttributeError):
            pass

    def get(self, kind: str, key: str):
        return self.data[kind].get(key)

    def put(self, kind: str, key: str, value) -> None:
        with self.lock:
            if kind == "c":
                self.data["c"] = value
            elif len(self.data[kind]) < STORE_MAX:
                self.data[kind][key] = value
            else:
                return
            self.dirty = True

    def flush(self) -> None:
        with self.lock:
            if not self.dirty:
                return
            text = json.dumps({k: (dict(v) if isinstance(v, dict) else v) for k, v in self.data.items()})
            self.dirty = False
        _atomic_write(self.path, text)


_STORES: dict = {}
_STORES_LOCK = threading.Lock()


def _store(face: FontFace) -> _FaceStore | None:
    key = (str(face.path), face.index, _mtime(face.path))
    with _STORES_LOCK:
        st = _STORES.get(key)
        if st is None:
            try:
                st = _STORES[key] = _FaceStore(_cache_file(face, "face"))
            except OSError as e:
                log.warning("font match cache unavailable (%s)", describe(e))
                return None
        return st


def flush_cache() -> None:
    """Write the prefilter data measured since the last flush (the style phase calls it once per scene)."""
    with _STORES_LOCK:
        stores = list(_STORES.values())
    for st in stores:
        st.flush()


def _coverage(face: FontFace) -> frozenset[int]:
    """The face's cmap coverage, from the store when known."""
    key = (str(face.path), face.index, _mtime(face.path))
    hit = _COVER.get(key)
    if hit is not None:
        return hit
    st = _store(face)
    ranges = st.data.get("c") if st is not None else None
    try:   # codepoint ranges within Unicode
        ok = isinstance(ranges, list) and all(0 <= int(a) <= int(b) <= 0x10FFFF for a, b in ranges)
        cps = frozenset(c for a, b in ranges for c in range(int(a), int(b) + 1)) if ok else None
    except (TypeError, ValueError):
        cps = None
    if cps is None:
        cps = cmap(face)
        if st is not None and cps:
            srt, ranges = sorted(cps), []
            for c in srt:
                if ranges and ranges[-1][1] == c - 1:
                    ranges[-1][1] = c
                else:
                    ranges.append([c, c])
            st.put("c", "", ranges)
    _COVER[key] = cps
    return cps


_COVER: dict = {}
_RENDERS: contextvars.ContextVar = contextvars.ContextVar("font_match_renders", default=None)   # [count] per scene


def _wkey(weight: float) -> str:
    return repr(round(float(weight), 2))


def advances(face: FontFace, weight: float, chars) -> dict[str, float]:
    """Advance widths (R px) only — far cheaper than bitmaps; cached per (face, weight, character), on disk too."""
    fkey = (str(face.path), face.index, _mtime(face.path), round(float(weight), 2), R)
    st, wk = _store(face), _wkey(weight)
    out, todo = {}, []
    for ch in set(chars) - {"\n"}:
        v = _ADV.get(fkey + (ch,))
        if v is None and st is not None:
            v = st.get("a", wk + "|" + ch)
            if isinstance(v, (int, float)):
                _ADV[fkey + (ch,)] = v
            else:
                v = None
        if v is None:
            todo.append(ch)
        else:
            out[ch] = v
    if todo:
        with _FT_LOCK:
            font = _font(face, weight)
            for ch in todo:
                out[ch] = _ADV[fkey + (ch,)] = _length(font, ch, LIGA_OFF)
                if st is not None:
                    st.put("a", wk + "|" + ch, out[ch])
            _bounded(_ADV)
    return out


def _encode(g: _Glyph) -> list:
    return [g.adv, *g.box, g.ox, g.oy, g.cx, g.cy, int(g.ink), [v for part in g.parts for v in part]]


def _decode(v) -> _Glyph | None:
    try:
        adv, l, t, r, b, ox, oy, cx, cy, ink, flat = v
        parts = tuple((float(flat[i]), float(flat[i + 1])) for i in range(0, len(flat) - 1, 2))
        return _Glyph(float(adv), (float(l), float(t), float(r), float(b)), None, int(ox), int(oy), float(cx), float(cy),
                      parts, bool(ink))
    except (TypeError, ValueError):
        return None


def _render_glyph(font, ch: str) -> _Glyph:
    from PIL import Image
    g = _Glyph(_length(font, ch, LIGA_OFF), (0.0, 0.0, 0.0, 0.0))
    if ch.isspace():
        g.parts = ()
        return g
    mask, (ox, oy) = font.getmask2(ch, mode="L", anchor="ls")
    img = np.asarray(Image.Image()._new(mask)) if mask.size[0] and mask.size[1] else None
    if img is not None and img.max() > 0:
        wts = img.astype(np.float32)
        m = float(wts.sum())
        cols, rows = np.flatnonzero(img.max(0) > 63), np.flatnonzero(img.max(1) > 63)   # the ink, not the mask
        cols = cols if len(cols) else np.array([0, img.shape[1] - 1])
        rows = rows if len(rows) else np.array([0, img.shape[0] - 1])
        g.img, g.ox, g.oy = img, int(ox), int(oy)
        g.box = (float(ox + cols[0]), float(oy + rows[0]), float(ox + cols[-1] + 1), float(oy + rows[-1] + 1))
        g.cx = float((wts.sum(0) * (np.arange(img.shape[1]) + 0.5)).sum() / m) + ox
        g.cy = float((wts.sum(1) * (np.arange(img.shape[0]) + 0.5)).sum() / m) + oy
        g.ink = True
    else:
        g.parts = ()
    return g


def _with_parts(g: _Glyph) -> _Glyph:
    """The glyph with its solid parts measured (the prefilter's cap height); needs the bitmap once."""
    if g.parts is None:
        parts = ()
        n, _, st, _ = (cv2.connectedComponentsWithStats((g.img > 127).astype(np.uint8), connectivity=8)
                       if g.img is not None else (0, None, None, None))
        if n > 1:
            st = st[1:]
            st = st[st[:, cv2.CC_STAT_AREA] >= max(2, 0.01 * st[:, cv2.CC_STAT_AREA].max())]
            parts = tuple((float(g.oy + top), float(g.oy + top + h))
                          for top, h in zip(st[:, cv2.CC_STAT_TOP], st[:, cv2.CC_STAT_HEIGHT]))
        g.parts = parts
    return g


def _cap_r(parts) -> float:
    """`textstyle.cap_height`'s rule on glyph parts (R px): the 90th percentile height above the median part bottom
    of the parts sitting on it — dots, accents and descenders left out, so a family's weight barely moves it."""
    if not parts:
        return 0.0
    top, bottom = (np.array(v, np.float64) for v in zip(*parts))
    hh = bottom - top
    base = float(np.median(bottom))
    on = np.abs(bottom - base) <= max(0.03 * R, 0.08 * hh.max())
    return float(np.percentile(base - top[on] if on.any() else hh, 90))


def run_glyph(face: FontFace, weight: float, text: str) -> _Glyph:
    """A whole run shaped with the default features (ligatures, contextual forms): the raster's untracked path."""
    key = (str(face.path), face.index, _mtime(face.path), round(float(weight), 2), R, "run", text)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit[0]
    from PIL import Image
    with _FT_LOCK:
        font = _font(face, weight)
        g = _Glyph(_length(font, text, None), (0.0, 0.0, 0.0, 0.0))
        mask, (ox, oy) = font.getmask2(text, mode="L", anchor="ls")
    img = np.asarray(Image.Image()._new(mask)) if mask.size[0] and mask.size[1] else None
    if img is not None and img.max() > 0:
        g.img, g.ox, g.oy, g.ink = img, int(ox), int(oy), True
        g.box = (float(ox), float(oy), float(ox + img.shape[1]), float(oy + img.shape[0]))
    _CACHE.put(key, g, (img.nbytes if img is not None else 0) + 200)
    return g


def glyph_set(face: FontFace, weight: float, text: str, *, bitmaps: bool = True) -> _GlyphSet:
    """Advances, ink boxes, centroids, solid parts and (with `bitmaps`) bitmaps of the characters of `text` and the
    pair kerning of its neighbours, in `face` at `weight`, R px. Glyphs and pairs are cached one by one per (face,
    weight), so texts and search steps share them; metrics outlive their bitmaps in the cache."""
    fkey = (str(face.path), face.index, _mtime(face.path), round(float(weight), 2), R)
    chars = sorted(set(text) - {"\n"})
    pairs = sorted({a + b for line in text.split("\n") for a, b in zip(line, line[1:])})
    st, wk = (None, "") if bitmaps else (_store(face), _wkey(weight))   # metrics only: the disk store too
    glyphs, kern = {}, {}
    missing = []
    for ch in chars:
        g = None if bitmaps else _METRICS.get(fkey + (ch,))
        if (g is None or g.parts is None) and st is not None:
            v = st.get("g", wk + "|" + ch)
            stored = _decode(v) if v is not None else None
            if stored is not None:
                g = _METRICS[fkey + (ch,)] = stored
        if g is None or g.parts is None:   # metrics without parts: from the bitmap, when it is still cached
            hit = _CACHE.get(fkey + (ch,))
            g = hit[0] if hit is not None else None
            if g is not None and not bitmaps:
                g = _METRICS[fkey + (ch,)] = _with_parts(g).metrics()
                if st is not None:
                    st.put("g", wk + "|" + ch, _encode(g))
        if g is None:
            missing.append(ch)
        else:
            glyphs[ch] = g
    todo = []
    for p in pairs:
        if (fkey + (p,)) in _KERN:
            continue
        v = st.get("k", wk + "|" + p) if st is not None else None
        if isinstance(v, (int, float)):
            _KERN[fkey + (p,)] = v
        else:
            todo.append(p)
    if missing or todo:
        with _FT_LOCK:
            font = _font(face, weight)
            for ch in missing:
                g = glyphs[ch] = _render_glyph(font, ch)
                if not bitmaps:
                    _with_parts(g)
                _CACHE.put(fkey + (ch,), g, (g.img.nbytes if g.img is not None else 0) + 200)
                _METRICS[fkey + (ch,)] = g.metrics()
                if st is not None:
                    st.put("g", wk + "|" + ch, _encode(g))
            _bounded(_METRICS)
            for p in todo:
                a = glyphs[p[0]].adv if p[0] in glyphs else _length(font, p[0], LIGA_OFF)
                b = glyphs[p[1]].adv if p[1] in glyphs else _length(font, p[1], LIGA_OFF)
                _KERN[fkey + (p,)] = _length(font, p, LIGA_OFF) - a - b
                if st is not None:
                    st.put("k", wk + "|" + p, _KERN[fkey + (p,)])
            _bounded(_KERN)
    for p in pairs:
        kern[p] = _KERN.get(fkey + (p,), 0.0)
    return _GlyphSet(glyphs, kern)


# --- layout (the composer's model) -------------------------------------------------------------------------------

@dataclass
class _Fonts:
    primary: FontFace
    weight: float
    fallback: FontFace | None = None
    fb_weight: float = 400.0
    fb_scale: float = 1.0
    fb_chars: frozenset = frozenset()  # characters drawn by the fallback


@dataclass
class _Layout:
    """Pen positions (R px, tracking-free) of every character, per line; and the glyph sets."""
    lines: list[list[tuple[str, float, bool]]]     # (char, pen x, uses fallback)
    pset: _GlyphSet
    fset: _GlyphSet | None
    fonts: _Fonts
    text: str = ""
    fbx: list | None = None      # per character: the fallback advances before it at scale 1 (pen x = rest + scale·fbx)

    def glyph(self, ch: str, fb: bool) -> _Glyph:
        return (self.fset if fb else self.pset).glyphs[ch]


def _layout(text: str, fonts: _Fonts, *, bitmaps: bool = True) -> _Layout:
    lines = text.split("\n")
    fb_text = "".join(c for c in text if c in fonts.fb_chars)
    pset = glyph_set(fonts.primary, fonts.weight, "\n".join("".join(c for c in ln if c not in fonts.fb_chars) for ln in lines),
                     bitmaps=bitmaps)
    fset = glyph_set(fonts.fallback, fonts.fb_weight, fb_text, bitmaps=bitmaps) if fonts.fallback and fb_text else None
    return _layout_with(fonts, text, pset, fset)


def _layout_with(fonts: _Fonts, text: str, pset: _GlyphSet, fset: _GlyphSet | None) -> _Layout:
    lines = text.split("\n")
    out, fbx = [], []
    for line in lines:
        row, xs, x, xf, prev = [], [], 0.0, 0.0, None
        for ch in line:
            fb = ch in fonts.fb_chars and fset is not None
            sc = fonts.fb_scale if fb else 1.0
            if prev is not None:
                pch, pfb, pg = prev
                psc = fonts.fb_scale if pfb else 1.0
                x += pg.adv * psc
                xf += pg.adv if pfb else 0.0
                if pfb == fb:
                    k = (fset if fb else pset).kern.get(pch + ch, 0.0)
                    x += k * sc
                    xf += k if fb else 0.0
            row.append((ch, x, fb))
            xs.append(xf)
            prev = (ch, fb, (fset if fb else pset).glyphs[ch])
        out.append(row)
        fbx.append(xs)
    return _Layout(out, pset, fset, fonts, text, fbx)


@dataclass
class _Base:
    """A composed candidate, area-resized from R px to the base scale."""
    img: np.ndarray                    # float32
    q: tuple[float, float]             # base px per R px (x, y)
    origin: tuple[float, float]        # line-0 pen on its baseline, base edge coordinates
    centroid: tuple[float, float]      # ink centroid, base pixel-centre coordinates
    s: float                           # native size this base was composed for


def _compose(lay: _Layout, t_em: float, pitch_r: float, q: float, whole: bool = False) -> _Base | None:
    """The candidate at R px, area-resized by q. whole: every line one run with ligatures (tracking 0, no fallback)."""
    tr = t_em * R
    placed = []
    if whole:
        for li, line in enumerate(lay.text.split("\n")):
            g = run_glyph(lay.fonts.primary, lay.fonts.weight, line) if line.strip() else None
            if g is not None and g.img is not None:
                placed.append((g.img, float(g.ox), li * pitch_r + g.oy))
    for li, row in enumerate(lay.lines if not whole else ()):
        base_y = li * pitch_r
        for j, (ch, x, fb) in enumerate(row):
            g = lay.glyph(ch, fb)
            if g.img is None:
                continue
            img, sc = g.img, lay.fonts.fb_scale if fb else 1.0
            if fb and abs(sc - 1.0) > 1e-3:
                img = _scaled(g, sc)
            px, py = x + j * tr + g.ox * sc, base_y + g.oy * sc
            placed.append((img, px, py))
    if not placed:
        return None
    x0 = math.floor(min(p[1] for p in placed)) - 2
    y0 = math.floor(min(p[2] for p in placed)) - 2
    x1 = math.ceil(max(p[1] + p[0].shape[1] for p in placed)) + 2
    y1 = math.ceil(max(p[2] + p[0].shape[0] for p in placed)) + 2
    canvas = np.zeros((y1 - y0, x1 - x0), np.uint8)
    for img, px, py in placed:
        cx, cy = int(round(px - x0)), int(round(py - y0))
        h, w = img.shape
        sub = canvas[cy:cy + h, cx:cx + w]
        np.maximum(sub, img[:sub.shape[0], :sub.shape[1]], out=sub)
    H, W = canvas.shape
    ow, oh = max(1, int(round(W * q))), max(1, int(round(H * q)))
    small = cv2.resize(canvas, (ow, oh), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    qx, qy = ow / W, oh / H
    m = cv2.moments(small)
    if m["m00"] <= 1e-6:
        return None
    return _Base(small, (qx, qy), (-x0 * qx, -y0 * qy), (m["m10"] / m["m00"], m["m01"] / m["m00"]), 0.0)


_SCALED: dict = {}


def _scaled(g: _Glyph, sc: float) -> np.ndarray:
    key = (id(g.img), round(sc, 4))
    hit = _SCALED.get(key)
    if hit is not None and hit[0] is g.img:
        return hit[1]
    h, w = g.img.shape
    out = cv2.resize(g.img, (max(1, int(round(w * sc))), max(1, int(round(h * sc)))), interpolation=cv2.INTER_AREA)
    if len(_SCALED) > 4096:
        _SCALED.clear()
    _SCALED[key] = (g.img, out)
    return out


# --- the observation -------------------------------------------------------------------------------------------

def _a01(alpha) -> np.ndarray:
    a = np.asarray(alpha, np.float32)
    if a.ndim == 3:
        a = a[..., -1]
    return a / 255.0 if a.size and a.max() > 1.5 else a


@dataclass
class _Obs:
    alpha: np.ndarray                  # native coverage (the box)
    text: str                          # the matched text (maybe a run of whole words)
    full_text: str
    line0: int                         # index of the matched text's first line in full_text
    char0: int                         # index of its first character in that line
    crop: tuple[int, int]              # native x0, y0 of the working crop
    O: np.ndarray                      # working image
    f: float                           # working px per native px (vertical)
    fx: float                          # … horizontal (rounding)
    centroid: tuple[float, float]
    cap: float                         # cap height of O
    h: float                           # native tight height
    w: float                           # native tight width
    ratio: float | None
    centres: list[float] | None        # native x of every non-space character of `text`
    pitch: float                       # native line pitch
    n_lines: int
    chars: frozenset = field(default_factory=frozenset)
    pieces: list | None = None         # per line: (cx, x0, x1, mass) of the glyph pieces, native

    def __post_init__(self):
        self.cols, self.rows = self.O.sum(0), self.O.sum(1)   # profiles candidates are registered on

    @property
    def positional(self) -> bool:
        """Glyph positions are known: Task 9's centres, or enough separate pieces to give each candidate its own."""
        if self.centres is not None:
            return True
        n = len([c for c in self.text if not c.isspace()])
        return self.pieces is not None and sum(len(p) for p in self.pieces) >= max(3, PIECES_MIN * n)


def _tight(a: np.ndarray, thr: float = 0.25) -> tuple[int, int, int, int] | None:
    ys, xs = np.nonzero(a > thr)
    if not len(xs):
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def _line_bands(a: np.ndarray, n: int) -> list[tuple[int, int]] | None:
    from ..analyze.textstyle import _components, _line_groups
    _, st, _ = _components(a, 0.5)
    if len(st) < n:
        return None
    groups = _line_groups(st, n)
    if len(groups) != n:
        return None
    bands = []
    for g in groups:
        top = int(st[g, cv2.CC_STAT_TOP].min())
        bottom = int((st[g, cv2.CC_STAT_TOP] + st[g, cv2.CC_STAT_HEIGHT]).max())
        bands.append((top, bottom))
    return bands


def _baselines(a: np.ndarray, n: int) -> list[float] | None:
    from ..analyze.textstyle import _components, _line_groups
    _, st, _ = _components(a, 0.5)
    if len(st) < n:
        return None
    out = []
    for g in _line_groups(st, n):
        out.append(float(np.median(st[g, cv2.CC_STAT_TOP] + st[g, cv2.CC_STAT_HEIGHT])))
    return sorted(out)


def _word_window(a: np.ndarray, band: tuple[int, int], line: str) -> tuple[int, int, int, int] | None:
    """(x0, x1, char0, char1) of the longest run of whole words of `line` with at most MAX_CHARS characters, found
    from the widest column gaps of the band. None when the gaps do not split the line into its words."""
    words = [(m.start(), m.end()) for m in re.finditer(r"\S+", line)]
    if len(words) < 2:
        return None
    prof = a[band[0]:band[1]].max(0) > 0.25
    cols = np.flatnonzero(prof)
    if not len(cols):
        return None
    gaps = []
    run = None
    for x in range(cols[0], cols[-1] + 1):
        if not prof[x]:
            run = [x, x + 1] if run is None else [run[0], x + 1]
        elif run is not None:
            gaps.append(tuple(run))
            run = None
    if len(gaps) < len(words) - 1:
        return None
    seps = sorted(sorted(gaps, key=lambda g: g[1] - g[0])[-(len(words) - 1):])
    widths = sorted(g[1] - g[0] for g in gaps)
    if len(gaps) > len(words) - 1 and (seps and min(s[1] - s[0] for s in seps) <= widths[-len(words)]):
        return None   # the word gaps are not clearly the widest
    edges = [cols[0]] + [x for g in seps for x in g] + [cols[-1] + 1]
    spans = [(edges[2 * i], edges[2 * i + 1]) for i in range(len(words))]
    best = None
    for i in range(len(words)):
        n = 0
        for j in range(i, len(words)):
            n += words[j][1] - words[j][0]
            if n > MAX_CHARS and j > i:
                break
            if best is None or n > best[0]:
                best = (n, i, j)
    _, i, j = best
    return spans[i][0], spans[j][1], words[i][0], words[j][1]


def _observe(alpha, text: str, ratio: float | None, centres) -> _Obs:
    """The observation at the working scale: the tight box WORK_H px high plus a margin; text over MAX_CHARS
    characters cut to its longest line's longest run of whole words (the rest of the alpha masked); the stroke ratio
    measured when not given; centres kept only when there is one per non-space character."""
    from ..analyze.textstyle import cap_height, stroke_width
    a = _a01(alpha)
    text = (text or "").replace("\r", "")
    full = text
    lines = text.split("\n")
    nonspace = [c for c in text if not c.isspace()]
    if not nonspace:
        raise ValueError("no text to match")
    if len(text) > MAX_TEXT:
        raise ValueError(f"text longer than {MAX_TEXT} characters")
    box = _tight(a)
    if box is None:
        raise ValueError("no glyph coverage")
    n_all = len(lines)
    line0, char0, region = 0, 0, (0, 0, a.shape[1], a.shape[0])
    if len(nonspace) > MAX_CHARS:
        # the longest line, then its longest run of whole words
        bands = _line_bands(a, n_all) if n_all > 1 else [(box[1], box[3])]
        if bands is not None:
            li = max(range(n_all), key=lambda i: len(lines[i].replace(" ", "")))
            band = bands[li]
            sub_text, x0, x1 = lines[li], 0, a.shape[1]
            win = _word_window(a, band, lines[li]) if len(lines[li].replace(" ", "")) > MAX_CHARS else None
            if win is not None:
                x0, x1, c0, c1 = win
                gap = max(2, int(0.05 * (band[1] - band[0])))
                x0, x1 = max(0, x0 - gap), min(a.shape[1], x1 + gap)
                sub_text, char0 = lines[li][c0:c1], c0
            if centres is not None:
                idx = sum(len(lines[i].replace(" ", "")) for i in range(li)) + len(lines[li][:char0].replace(" ", ""))
                centres = list(centres)[idx:idx + len(sub_text.replace(" ", ""))]
            line0 = li
            pad = max(2, int(0.1 * (band[1] - band[0])))
            region = (x0, max(0, band[0] - pad), x1, min(a.shape[0], band[1] + pad))
            masked = np.zeros_like(a)
            masked[region[1]:region[3], region[0]:region[2]] = a[region[1]:region[3], region[0]:region[2]]
            a_used, text = masked, sub_text
            box = _tight(a_used) or box
        else:
            a_used = a
    else:
        a_used = a
    lines = text.split("\n")
    n = len(lines)
    bx0, by0, bx1, by1 = box
    h, w = by1 - by0, bx1 - bx0
    f = WORK_H / max(h, 1)
    mg = int(math.ceil(8.0 / f)) + 1
    x0, y0 = bx0 - mg, by0 - mg
    crop = np.zeros((h + 2 * mg, w + 2 * mg), np.float32)
    sx0, sy0 = max(0, x0), max(0, y0)
    sx1, sy1 = min(a.shape[1], bx1 + mg), min(a.shape[0], by1 + mg)
    crop[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = a_used[sy0:sy1, sx0:sx1]
    ow, oh = max(1, int(round(crop.shape[1] * f))), max(1, int(round(crop.shape[0] * f)))
    O = cv2.resize(crop, (ow, oh), interpolation=cv2.INTER_AREA if f < 1 else cv2.INTER_LINEAR)
    m = cv2.moments(O)
    if m["m00"] <= 1e-6:
        raise ValueError("no glyph coverage")
    if ratio is None or not (isinstance(ratio, (int, float)) and math.isfinite(ratio) and ratio > 0):
        sw, cap = stroke_width(crop), cap_height(crop, n)
        ratio = sw / cap if cap > 0 and sw > 0 else None
    if centres is not None:
        centres = [float(c) for c in centres]
        if len(centres) != len(text.replace(" ", "").replace("\n", "")) or not all(math.isfinite(c) for c in centres):
            centres = None
    pitch = float(a.shape[0]) / max(1, n_all)
    if n > 1:
        bl = _baselines(crop, n)
        if bl is not None and len(bl) == n:
            pitch = float(np.mean(np.diff(bl)))
    return _Obs(a, text, full, line0, char0, (x0, y0), O.astype(np.float32), oh / crop.shape[0], ow / crop.shape[1],
                (m["m10"] / m["m00"], m["m01"] / m["m00"]), cap_height(O, n), float(h), float(w), ratio, centres, pitch, n,
                frozenset(c for c in text if not c.isspace()), _pieces(crop, n, x0))


def _pieces(crop: np.ndarray, n: int, x0: int) -> list | None:
    """Connected pieces of the glyphs per line: (α-weighted x centre, left, right, mass), native x."""
    from ..analyze.textstyle import _components, _line_groups
    lab, st, ids = _components(crop, 0.35)
    if not len(st):
        return None
    xs = np.arange(crop.shape[1], dtype=np.float64) + 0.5
    mass = np.bincount(lab.ravel(), weights=crop.ravel().astype(np.float64), minlength=lab.max() + 1)
    mx = np.bincount(lab.ravel(), weights=(crop * xs).ravel().astype(np.float64), minlength=lab.max() + 1)
    out = []
    for g in _line_groups(st, n) if n > 1 else [np.arange(len(st))]:
        rows = []
        for i in g:
            k = ids[i]
            left = float(st[i, cv2.CC_STAT_LEFT])
            rows.append((mx[k] / max(mass[k], 1e-9) + x0, left + x0, left + float(st[i, cv2.CC_STAT_WIDTH]) + x0, mass[k]))
        out.append(np.array(sorted(rows), np.float64).reshape(-1, 4))
    return out if len(out) == n else None


# --- scoring ---------------------------------------------------------------------------------------------------

def _soft_iou(a: np.ndarray, b: np.ndarray) -> float:
    inter = float(np.minimum(a, b).sum())
    return inter / max(float(np.maximum(a, b).sum()), 1e-9)


def _shift(p: np.ndarray, q: np.ndarray, r: int) -> float:
    """The shift d (|d| ≤ r, sub-pixel) that best lays profile p on q (q[x] ≈ p[x − d]), by dot product."""
    n = len(p)
    v = np.array([float(np.dot(p[:n - d], q[d:])) if d >= 0 else float(np.dot(p[-d:], q[:n + d]))
                  for d in range(-r, r + 1)])
    i = int(np.argmax(v))
    if 0 < i < 2 * r:
        den = v[i - 1] - 2 * v[i] + v[i + 1]
        return i - r + (0.5 * (v[i - 1] - v[i + 1]) / den if den < 0 else 0.0)
    return float(i - r)


def _warp(obs: _Obs, base: _Base, s: float, shear_deg: float) -> tuple[np.ndarray, np.ndarray]:
    """The candidate on the working canvas: scaled to size s, sheared about its first baseline, centroids aligned,
    then registered on the observation's column and row profiles (REGISTER px at most) — a centroid moves when
    mass shifts between glyphs (a weight step, Latin against a fallback's Hangul) and drags every glyph with it."""
    mscale = (s / base.s) * max(obs.f, 1.0)
    k = math.tan(math.radians(shear_deg))
    ub = base.origin[1] - 0.5
    cx, cy = base.centroid
    tx = obs.centroid[0] - mscale * cx + k * mscale * (cy - ub)
    ty = obs.centroid[1] - mscale * cy
    M = np.float32([[mscale, -k * mscale, k * mscale * ub + tx], [0, mscale, ty]])
    size = (obs.O.shape[1], obs.O.shape[0])
    C = cv2.warpAffine(base.img, M, size, flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    dx, dy = _shift(C.sum(0), obs.cols, REGISTER[0]), _shift(C.sum(1), obs.rows, REGISTER[1])
    if abs(dx) > 0.05 or abs(dy) > 0.05:
        M[0, 2] += dx
        M[1, 2] += dy
        C = cv2.warpAffine(base.img, M, size, flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    return C, M


def _origin(obs: _Obs, base: _Base, M: np.ndarray) -> tuple[float, float]:
    """Native box coordinates (edge) of the matched text's pen on its first baseline."""
    u = np.array([base.origin[0] - 0.5, base.origin[1] - 0.5, 1.0])
    X, Y = M @ u
    return obs.crop[0] + (X + 0.5) / obs.fx, obs.crop[1] + (Y + 0.5) / obs.f


# --- per-family fit -----------------------------------------------------------------------------------------

def _bucket(w: float) -> int:
    return min(900, max(100, int(w / 100 + 0.5) * 100))


class _Family:
    """A candidate family: its files, weight range and (for Hangul it lacks) the fallback."""

    def __init__(self, family: str, registry: FontRegistry, chars: frozenset):
        self.family, self.registry = family, registry
        faces = registry.faces(family) or ((registry.face(family),) if registry.face(family) else ())
        if not faces:
            raise LookupError(f"no font for {family!r}")
        self.faces = tuple(faces)
        self.variable = any(f.weight_range[0] < f.weight_range[1] for f in self.faces)
        if self.variable:
            v = next(f for f in self.faces if f.weight_range[0] < f.weight_range[1])
            self.range = weight_range(v)
            self.statics = ()
        else:
            self.range = None
            self.statics = tuple(sorted({max(WEIGHTS[0], min(WEIGHTS[1], f.weight_range[0])) for f in self.faces}))
        primary = self.face(400)
        covered = _coverage(primary)
        self.missing = frozenset(c for c in chars if ord(c) not in covered)
        self.hangul_missing = frozenset(c for c in self.missing if _HANGUL.match(c))
        self.category = primary.category

    def face(self, weight: float) -> FontFace:
        return min(self.faces, key=lambda f: (0 if f.weight_range[0] <= weight <= f.weight_range[1] else
                                              min(abs(weight - f.weight_range[0]), abs(weight - f.weight_range[1]))))

    def clamp(self, weight: float) -> float:
        if self.variable:
            return float(min(max(weight, self.range[0]), self.range[1]))
        return float(min(self.statics, key=lambda s: abs(s - weight)))

    def fonts(self, weight: float, fb: tuple[str, float, float] | None = None, *, scale: float | None = None,
              fb_weight: float | None = None) -> _Fonts:
        """The faces at `weight`; for Hangul the primary lacks, the registry's fallback at the weight's bucket
        (or `fb_weight`) and `scale` (default 0.92) — or exactly `fb` (family, weight, scale)."""
        face = self.face(weight)
        w = min(max(weight, face.weight_range[0]), face.weight_range[1])
        if not self.hangul_missing:
            return _Fonts(face, w)
        if fb is None:
            name, fw = self.registry.hangul_fallback(self.family, _bucket(weight if fb_weight is None else fb_weight))
            fb = (name, fw, FALLBACK_SCALE if scale is None else scale)
        fam, fw, scale = fb
        fface = self.registry.face(fam, int(fw))
        if fface is None:
            return _Fonts(face, w)
        fw = min(max(fw, fface.weight_range[0]), fface.weight_range[1])
        fcm = _coverage(fface)
        return _Fonts(face, w, fface, fw, scale, frozenset(c for c in self.hangul_missing if ord(c) in fcm))


@functools.lru_cache(maxsize=4096)
def _file_sha(path: str, mtime: float) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cache_parts() -> dict:
    """Everything a cached measure depends on besides the font file: the rasterising libraries' versions (Pillow and
    the FreeType / raqm / HarfBuzz / FriBiDi it reports, fontTools for coverage), the measuring constants, the
    keepframe version and a hash of the measuring code."""
    import inspect
    import PIL
    import fontTools
    from PIL import features
    from .. import __version__ as kf_version
    from ..analyze import textstyle
    from . import raster

    def version(name: str):
        try:
            return features.version(name)
        except Exception:   # not built in
            return None

    code = hashlib.sha256()
    for fn in (_measure_calibration, _render_glyph, _with_parts, _compose, _layout, _layout_with, glyph_set, advances,
               _coverage, raster._length, raster.cmap, textstyle.stroke_width, textstyle.cap_height,
               textstyle._components, textstyle._line_groups):
        try:
            code.update(inspect.getsource(fn).encode())
        except (OSError, TypeError):   # no source shipped: the keepframe version stands for it
            code.update(getattr(fn, "__qualname__", repr(fn)).encode())
    return {"pillow": PIL.__version__, "freetype": version("freetype2"), "raqm": version("raqm"),
            "harfbuzz": version("harfbuzz"), "fribidi": version("fribidi"), "fonttools": fontTools.version,
            "keepframe": kf_version, "code": code.hexdigest(),
            "constants": [R, CALIB_TEXT, list(WEIGHTS), FIXED_WEIGHT, ADV_STEP, list(LIGA_OFF), CALIB_VERSION,
                          STORE_VERSION]}


def _cache_tag(parts: dict) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:20]


@functools.lru_cache(maxsize=1)
def _current_tag() -> str:
    return _cache_tag(_cache_parts())


def _cache_file(face: FontFace, kind: str) -> Path:
    """A per-font file next to the font subsets (`css.cache_root()/match`), keyed by the font file's sha256 (the
    manifest's for bundled fonts, hashed here for any other file) and `_cache_parts`, so what a file holds never
    depends on which library or code version measured it."""
    from .css import cache_root
    sha = face.sha256 if face.source == "bundled" and face.sha256 else _file_sha(str(face.path), _mtime(face.path))
    return cache_root() / "match" / f"{kind}-{sha}-{face.index}-{_current_tag()}.json"


def _atomic_write(path: Path, text: str) -> None:
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
        tmp = None
    except OSError as e:   # read-only cache: recomputed next time
        log.warning("font match cache unavailable (%s)", describe(e))
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _calib_read(path: Path) -> tuple[tuple[float, float], ...] | None:
    try:
        pts = json.loads(path.read_text(encoding="utf-8"))["points"]
        out = tuple((float(w), float(r)) for w, r in pts)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    ok = 1 <= len(out) <= 3 and all(math.isfinite(w) and math.isfinite(r) and r > 0 for w, r in out)
    return out if ok else None


def _calib_write(path: Path, points: tuple) -> None:
    _atomic_write(path, json.dumps({"points": [list(p) for p in points]}))


@functools.lru_cache(maxsize=1024)
def _calibration(face: FontFace, mtime: float) -> tuple[tuple[float, float], ...]:
    """(weight, stroke ratio) of CALIB_TEXT at the ends (and middle) of the face's weight range; cached on disk per
    font file (the prefilter needs every family's on a process's first match)."""
    try:
        path = _cache_file(face, "calib")
    except OSError as e:
        log.warning("font calibration cache unavailable (%s)", describe(e))
        return _measure_calibration(face)
    hit = _calib_read(path)
    if hit is None:
        hit = _measure_calibration(face)
        if hit:
            _calib_write(path, hit)
    return hit


def _measure_calibration(face: FontFace) -> tuple[tuple[float, float], ...]:
    from ..analyze.textstyle import cap_height, stroke_width
    lo, hi = weight_range(face)
    weights = (lo, (lo + hi) / 2, hi) if hi > lo else (lo,)
    out = []
    for w in weights:
        lay = _layout(CALIB_TEXT, _Fonts(face, w))
        base = _compose(lay, 0.0, 0.0, 48.0 / R)
        if base is None:
            continue
        sw, cap = stroke_width(base.img), cap_height(base.img)
        if sw > 0 and cap > 0:
            out.append((float(w), sw / cap))
    return tuple(out)


def _ratio_range(fam: _Family) -> tuple[float, float] | None:
    pts = [r for face in fam.faces for _, r in _calibration(face, _mtime(face.path))]
    return (min(pts), max(pts)) if pts else None


def _seed_weight(fam: _Family, ratio: float | None) -> float:
    if not fam.variable:
        if ratio is None:
            return fam.clamp(400)
        best = None
        for face in fam.faces:
            for w, r in _calibration(face, _mtime(face.path)):
                if best is None or abs(r - ratio) < best[0]:
                    best = (abs(r - ratio), w)
        return fam.clamp(best[1] if best else 400)
    if ratio is None:
        return fam.clamp(400)
    face = fam.face((fam.range[0] + fam.range[1]) / 2)
    pts = sorted(_calibration(face, _mtime(face.path)))
    if len(pts) < 2:
        return fam.clamp(400)
    ws, rs = [p[0] for p in pts], list(np.maximum.accumulate([p[1] for p in pts]))
    if rs[-1] <= rs[0]:
        return fam.clamp(400)
    return fam.clamp(float(np.interp(ratio, rs, ws)))


class _Fitter:
    def __init__(self, obs: _Obs, fam: _Family):
        self.obs, self.fam = obs, fam
        self.fb: tuple[str, float, float] | None = None   # pinned fallback (family, weight, scale)
        self.fb_scale: float | None = None                # … or only its scale
        self.phi: float | None = None                     # the last fallback scale the centres gave
        self.layouts: dict = {}

    def fonts(self, w: float) -> _Fonts:
        return self.fam.fonts(w, self.fb, scale=self.fb_scale)

    def layout(self, w: float) -> _Layout:
        key = (round(w, 2), self.fb, self.fb_scale)
        lay = self.layouts.get(key)
        if lay is None:
            lay = self.layouts[key] = _layout(self.obs.text, self.fonts(w))
        return lay

    def tracking_ls(self, lay: _Layout, s: float, shear: float, t: float = 0.0) -> float | None:
        fit = _centres_ls(self.obs, lay, s, shear, t, fit_scale=self.fb is None)
        self.phi = None if fit is None else fit[2]
        return None if fit is None else fit[0]

    def settle(self, w: float, s: float, shear: float, t: float) -> tuple[_Layout, float]:
        """The layout at w and the tracking from the centres; a Hangul fallback takes the scale they give."""
        lay = self.layout(w)
        tt = self.tracking_ls(lay, s, shear, t)
        if tt is None:
            return lay, t
        if self.fb is None and self.phi is not None and abs(self.phi - lay.fonts.fb_scale) > 0.003:
            self.fb_scale = round(self.phi, 3)
            lay = self.layout(w)
            t2 = self.tracking_ls(lay, s, shear, tt)
            tt = tt if t2 is None else t2
        return lay, tt

    def base(self, lay: _Layout, t: float, s: float, whole: bool = False) -> _Base | None:
        """A candidate render (counted against the scene's work cap)."""
        counter = _RENDERS.get()
        if counter is not None:
            counter[0] += 1
        obs = self.obs
        pitch_r = obs.pitch * R / max(s, 1e-6)
        q = s * min(obs.f, 1.0) / R
        b = _compose(lay, 0.0 if whole else t, pitch_r, q, whole and lay.fset is None)
        if b is not None:
            b.s = s
        return b

    def size_from_cap(self, base: _Base, shear: float = 0.0) -> float:
        """The size whose cap height (measured like the observation's, on the working canvas) matches."""
        from ..analyze.textstyle import cap_height
        C, _ = _warp(self.obs, base, base.s, shear)
        cap = cap_height(C, self.obs.n_lines)
        if cap <= 0 or self.obs.cap <= 0:
            return base.s
        return base.s * self.obs.cap / cap

    def score(self, base: _Base, s: float, shear: float) -> float:
        C, _ = _warp(self.obs, base, s, shear)
        return _soft_iou(C, self.obs.O)


def _piece_centres(obs: _Obs, lay: _Layout, s: float, t: float) -> list[float | None] | None:
    """The candidate's own glyph centres: every observed piece goes to the character whose predicted ink span (the
    line's predicted ink extent stretched onto the observed one) holds its centre; a character's centre is its
    pieces' α-weighted mean (None when it got none)."""
    if obs.pieces is None or len(obs.pieces) != len(lay.lines):
        return None
    out = []
    for row, pcs in zip(lay.lines, obs.pieces):
        spans = []
        for j, (ch, x, fb) in enumerate(row):
            if ch.isspace():
                continue
            g = lay.glyph(ch, fb)
            sc = lay.fonts.fb_scale if fb else 1.0
            px = x + j * t * R
            spans.append(((px + g.box[0] * sc) * s / R, (px + g.box[2] * sc) * s / R, (px + g.cx * sc) * s / R))
        if not spans or not len(pcs):
            out += [None] * len(spans)
            continue
        p0, p1 = min(sp[0] for sp in spans), max(sp[1] for sp in spans)
        o0, o1 = float(pcs[:, 1].min()), float(pcs[:, 2].max())
        k = (p1 - p0) / max(o1 - o0, 1e-6)
        acc = [[0.0, 0.0] for _ in spans]
        for cx, _, _, m in pcs:
            u = p0 + (cx - o0) * k
            inside = [i for i, sp in enumerate(spans) if sp[0] <= u <= sp[1]]
            i = (min(inside, key=lambda i: abs(spans[i][2] - u)) if inside
                 else min(range(len(spans)), key=lambda i: abs(spans[i][2] - u)))
            acc[i][0] += m * cx
            acc[i][1] += m
        out += [a / m if m > 0 else None for a, m in acc]
    return out


def _centres_fit(obs: _Obs, lay: _Layout, s: float, shears, t: float = 0.0, *,
                 fit_scale: bool = True) -> list[tuple[float, float, float | None]] | None:
    """Per shear: tracking (em, clamped), RMS residual (px) and fallback scale (None when not solved) of the
    least-squares fit of the glyph centres, c_j = a_line + (pen_j + cx_j)·s/R − k·cy_j·s/R + j·t·s, where j counts
    every character of its line and a Hangul fallback's advances and glyphs scale by φ (pen = rest + φ·fbx), which
    is solved too (linear) when the layout has one and `fit_scale`. The centres are Task 9's, else the candidate's
    own from the pieces (`_piece_centres`, t: the tracking to place them with)."""
    c = obs.centres
    if c is None:
        c = _piece_centres(obs, lay, s, t) if obs.positional else None
        if c is None or sum(v is not None for v in c) < 3:
            return None
    has_fb = lay.fset is not None and lay.fbx is not None
    phi0 = lay.fonts.fb_scale if has_fb else 1.0
    rows, cs, P, Q, ZP, ZF, i = [], [], [], [], [], [], 0
    n_lines = len(lay.lines)
    for li, row in enumerate(lay.lines):
        for j, (ch, x, fb) in enumerate(row):
            if ch.isspace():
                continue
            if i >= len(c):
                return None
            if c[i] is None:
                i += 1
                continue
            g = lay.glyph(ch, fb)
            xf = lay.fbx[li][j] if has_fb else 0.0
            xp = x - phi0 * xf
            r = [0.0] * n_lines + [j * s]
            r[li] = 1.0
            rows.append(r)
            cs.append(c[i])
            if fb:
                P.append(xp), Q.append(xf + g.cx), ZP.append(0.0), ZF.append(g.cy)
            else:
                P.append(xp + g.cx), Q.append(xf), ZP.append(g.cy), ZF.append(0.0)
            i += 1
    if i != len(c) or len(rows) < n_lines + 1:
        return None
    A0, cs = np.array(rows), np.array(cs)
    P, Q, ZP, ZF = (np.array(v) * s / R for v in (P, Q, ZP, ZF))
    if not A0[:, -1].any():
        return None
    solve_phi = fit_scale and has_fb and bool(np.ptp(Q) > 1e-6)
    if not solve_phi:   # one solve for every shear: only the right-hand side depends on it
        ks = np.tan(np.radians(np.asarray(shears, np.float64)))
        Y = cs[:, None] - (P[:, None] - ks * ZP[:, None] + phi0 * (Q[:, None] - ks * ZF[:, None]))
        sol, _, rank, _ = np.linalg.lstsq(A0, Y, rcond=None)
        if rank < A0.shape[1]:
            return None
        rms = np.sqrt(np.mean((A0 @ sol - Y) ** 2, axis=0))
        return [(float(np.clip(sol[n_lines, j], *TRACK_LS)), float(rms[j]), None) for j in range(len(shears))]
    out = []
    for shear in shears:
        k = math.tan(math.radians(shear))
        if solve_phi:
            A = np.column_stack([A0, Q - k * ZF])
            y = cs - (P - k * ZP)
        else:
            A = A0
            y = cs - (P - k * ZP + phi0 * (Q - k * ZF))
        sol, _, rank, _ = np.linalg.lstsq(A, y, rcond=None)
        if rank < A.shape[1]:
            if not solve_phi:
                return None
            A = A0
            y = cs - (P - k * ZP + phi0 * (Q - k * ZF))
            sol, _, rank, _ = np.linalg.lstsq(A, y, rcond=None)
            if rank < A.shape[1]:
                return None
            phi = None
        else:
            phi = float(np.clip(sol[-1], *FALLBACK_SCALES)) if solve_phi else None
        tcol = n_lines
        out.append((float(np.clip(sol[tcol], *TRACK_LS)), float(np.sqrt(np.mean((A @ sol - y) ** 2))), phi))
    return out


def _centres_ls(obs: _Obs, lay: _Layout, s: float, shear: float, t: float = 0.0, *,
                fit_scale: bool = True) -> tuple[float, float, float | None] | None:
    fit = _centres_fit(obs, lay, s, (shear,), t, fit_scale=fit_scale)
    return None if fit is None else fit[0]


def _val(v) -> float:
    return v[0] if isinstance(v, tuple) else v


def _golden(fn, a: float, b: float, n: int, cache: dict, step: float = 5.0, key=None) -> float:
    """Golden-section maximum of fn on [a, b], evaluated on a grid (`key`, else multiples of `step`) once per point."""
    def ev(x):
        x = key(x) if key is not None else round(x / step) * step
        if x not in cache:
            cache[x] = fn(x)
        return cache[x]
    c, d = b - GOLDEN * (b - a), a + GOLDEN * (b - a)
    fc, fd = ev(c), ev(d)
    for _ in range(n):
        if _val(fc) >= _val(fd):
            b, d, fd = d, c, fc
            c = b - GOLDEN * (b - a)
            fc = ev(c)
        else:
            a, c, fc = c, d, fd
            d = a + GOLDEN * (b - a)
            fd = ev(d)
    return max(cache, key=lambda x: _val(cache[x]))


class _Search:
    """Coordinate descent for one family: weight, tracking (1-D search when there are no centres), size, shear."""

    def __init__(self, obs: _Obs, fam: _Family, start: tuple):
        """start: the prefilter's state (`_start`): weight, cap-height size, tracking, whole runs, fallback scale."""
        self.obs, self.fam = obs, fam
        self.ft = _Fitter(obs, fam)
        self.shear, self.score = 0.0, -1.0
        self.w, self.s, self.t, self.whole, self.ft.fb_scale = start

    def _at_weight(self, wx: float):
        ft, obs, s, shear = self.ft, self.obs, self.s, self.shear
        if self.whole:
            lay = ft.layout(wx)
            b = ft.base(lay, 0.0, s, whole=True)
            return ((ft.score(b, s, shear), wx, 0.0, s, b, ft.fb_scale) if b is not None
                    else (-1.0, wx, 0.0, s, None, ft.fb_scale))
        lay, tx = ft.settle(wx, s, shear, self.t) if obs.positional else (ft.layout(wx), self.t)
        b = ft.base(lay, tx, s)
        if b is None:
            return (-1.0, wx, tx, s, None, ft.fb_scale)
        return (ft.score(b, s, shear), wx, tx, s, b, ft.fb_scale)

    def round(self, rnd: int) -> float:
        ft, obs, fam = self.ft, self.obs, self.fam
        lay = ft.layout(self.w)
        if not self.whole and abs(self.t) <= LIG_T + 0.02 and lay.fset is None:   # untracked runs first?
            b1, b2 = ft.base(lay, self.t, self.s), ft.base(lay, 0.0, self.s, whole=True)
            if b1 is not None and b2 is not None and ft.score(b2, self.s, self.shear) > ft.score(b1, self.s, self.shear):
                self.whole, self.t = True, 0.0
        cache: dict = {}
        if fam.variable:
            grid = lambda x: fam.clamp(round(x / WEIGHT_STEP) * WEIGHT_STEP)

            def at(x: float) -> None:   # each grid weight evaluated once
                x = grid(x)
                if x not in cache:
                    cache[x] = self._at_weight(x)

            span = WEIGHT_SPAN[rnd]
            lo, hi = max(fam.range[0], self.w - span), min(fam.range[1], self.w + span)
            if hi - lo >= 1:
                _golden(self._at_weight, lo, hi, WEIGHT_ITERS[rnd], cache, key=grid)
                for _ in range(2 if rnd == 0 else 0):   # the best at an edge: one step further out
                    top = max(cache, key=lambda x: cache[x][0])
                    if top <= lo + 15 and lo > fam.range[0]:
                        lo = max(fam.range[0], lo - 100)
                        at(lo)
                    elif top >= hi - 15 and hi < fam.range[1]:
                        hi = min(fam.range[1], hi + 100)
                        at(hi)
                    else:
                        break
            at(self.w)
        else:
            for sw in fam.statics:
                cache[sw] = self._at_weight(sw)
        sc, w, t, s, b, ft.fb_scale = max(cache.values(), key=lambda v: v[0])
        if b is None:
            raise ValueError(f"{fam.family}: nothing drawn")
        shear = self.shear
        lay = ft.layout(w)
        if rnd == 0 and lay.fset is not None and ft.fb is None and not obs.positional:
            best_fs = ft.fb_scale   # the fallback's scale (with positions, the centres give it)
            for fs in FALLBACK_SEARCH:
                ft.fb_scale = fs
                bb = ft.base(ft.layout(w), t, s)
                if bb is not None:
                    v = ft.score(bb, s, shear)
                    if v > sc:
                        sc, b, best_fs = v, bb, fs
            ft.fb_scale = best_fs
            lay = ft.layout(w)
        if not obs.positional:   # tracking: 1-D search
            grid = (np.arange(TRACK_SEARCH[0], TRACK_SEARCH[1] + 1e-9, TRACK_STEP) if rnd == 0
                    else np.array([t - 0.005, t + 0.005]))
            for tv in grid:
                bb = ft.base(lay, float(tv), s)
                if bb is not None:
                    v = ft.score(bb, s, shear)
                    if v > sc:
                        sc, t, b = v, float(tv), bb

        whole = self.whole

        n_track = max((len(row) - 1 for row in lay.lines), default=0)

        def at_size(sx: float):   # tracking re-solved from the centres at each size (else: keep the ink width)
            if whole or (not obs.positional and n_track < 1):
                return ft.score(b, sx, shear), t, b
            tx = (ft.tracking_ls(lay, sx, shear, t) if obs.positional
                  else float(np.clip(t + obs.w * (1.0 / sx - 1.0 / s) / n_track, *TRACK_LS)))
            if tx is None or abs(tx - t) <= 0.002:
                return ft.score(b, sx, shear), t, b
            bb = ft.base(lay, tx, sx)
            return (ft.score(bb, sx, shear), tx, bb) if bb is not None else (-1.0, t, b)

        s0, best_m = s, 1.0
        for m in (SIZE_STEPS if rnd == 0 else (0.99, 1.01)):
            v, tv, bb = at_size(s0 * m)
            if v > sc:
                sc, best_m, t, b = v, m, tv, bb
        if rnd == 0 and best_m != 1.0:
            for m in (best_m - 0.01, best_m + 0.01):
                v, tv, bb = at_size(s0 * m)
                if v > sc:
                    sc, best_m, t, b = v, m, tv, bb
        s = s0 * best_m
        best_k = shear
        for kd in (SHEARS if rnd == 0 else ()):
            v = ft.score(b, s, kd)
            if v > sc:
                sc, best_k = v, kd
        for kd in ((best_k - SHEAR_FINE, best_k + SHEAR_FINE) if rnd == 0 else (best_k - 1.0, best_k + 1.0)):
            v = ft.score(b, s, kd)
            if v > sc:
                sc, best_k = v, kd
        shear = best_k
        if obs.positional and not whole:   # tracking follows the final size and shear
            tv = ft.tracking_ls(lay, s, shear, t)
            if tv is not None and abs(tv - t) > 0.002:
                bb = ft.base(lay, tv, s)
                if bb is not None:
                    v = ft.score(bb, s, shear)
                    if v >= sc - 0.002:
                        sc, t, b = v, tv, bb
        if not whole and abs(t) <= LIG_T and lay.fset is None:   # untracked: whole runs, ligatures on
            bb = ft.base(lay, 0.0, s, whole=True)
            if bb is not None:
                v = ft.score(bb, s, shear)
                if v > sc:
                    sc, t, b, whole = v, 0.0, bb, True
        self.whole = whole
        self.w, self.t, self.s, self.shear, self.b, self.score = w, t, s, shear, b, sc
        return sc

    def state(self) -> tuple:
        return self.w, self.s, self.t, self.shear, self.whole, self.b, self.score, self.ft.fb_scale

    def restore(self, state: tuple) -> None:
        self.w, self.s, self.t, self.shear, self.whole, self.b, self.score, self.ft.fb_scale = state

    def result(self) -> FontFit:
        ft, obs = self.ft, self.obs
        w, t, s, b = self.w, self.t, self.s, self.b
        shear = self.shear if abs(self.shear) >= SHEAR_MIN else 0.0
        if abs(b.s / s - 1) > 0.02:
            b = ft.base(ft.layout(w), t, s, whole=self.whole) or b
        C, M = _warp(obs, b, s, shear)
        ox, oy = _origin(obs, b, M)
        return _finish(obs, self.fam, ft, w, s, t, shear, _soft_iou(C, obs.O), ox, oy)


def _fit(obs: _Obs, family: str, registry: FontRegistry) -> FontFit:
    """One family's fit from the state the prefilter would start it at."""
    fam = _Family(family, registry, obs.chars)
    _, w, s, t, _ = _stage1(obs, fam)
    _, start = _start(obs, fam, w, s, t)
    search = _Search(obs, fam, start)
    search.round(0)
    first = search.state()
    try:
        search.round(1)
    except Exception as e:   # the first round's fit stands
        log.warning("font fit round 2 skipped %s: %s", family, describe(e))
        search.restore(first)
    return search.result()


def _finish(obs: _Obs, fam: _Family, ft: _Fitter, w: float, s: float, t: float, shear: float, score: float,
            ox: float, oy: float) -> FontFit:
    """FontFit with dx / dy of the full text in the observed box: the matched words' pen moved back over the
    characters before them, the matched line's baseline moved up to the first line's."""
    if obs.char0:
        line = obs.full_text.split("\n")[obs.line0]
        prefix = _layout(line[:obs.char0 + 1], ft.fonts(w))
        row = prefix.lines[0]
        ox -= row[-1][1] * s / R + obs.char0 * t * s
    if obs.line0:
        oy -= obs.line0 * float(obs.alpha.shape[0]) / max(1, len(obs.full_text.split("\n")))
    face = fam.face(w)
    n_all = max(1, len(obs.full_text.split("\n")))
    line_h = round(float(obs.alpha.shape[0]) / n_all, 4)
    try:
        dy = oy - first_baseline(face, s, line_h)
    except Exception:   # degenerate metrics: keep the baseline as the offset
        dy = oy
    return FontFit(fam.family, int(round(w)), round(float(s), 2), round(float(t), 4), round(float(shear), 1),
                   round(float(score), 4), round(float(ox), 2), round(float(dy), 2))


# --- prefilter ---------------------------------------------------------------------------------------------

def _eligible(registry: FontRegistry, chars: frozenset) -> list[str]:
    hangul = {c for c in chars if _HANGUL.match(c)}
    other = chars - hangul
    out = []
    for name in registry.families():
        face = registry.face(name)
        if face is None:
            continue
        cm = _coverage(face)
        if any(ord(c) not in cm for c in other):
            continue
        if hangul and not other and any(ord(c) not in cm for c in hangul):
            continue
        out.append(name)
    return out


def _stage1(obs: _Obs, fam: _Family) -> tuple[float, float, float, float, float | None]:
    """(distance, weight, size, tracking, centres RMS per em or None) from the layout of the text: variable families
    drawn at FIXED_WEIGHT with the seed weight's advances (`_reshaped`), static ones at their seed file; the size
    that matches the cap height (else the tight height); tracking from the glyph centres (least squares over five
    shears, whose RMS residual per em joins the distance) or else from the ink width; plus |log ink-width ratio|, a
    penalty for tracking beyond TRACK_PLAUSIBLE and the relative stroke-ratio distance to the family's calibrated
    range beyond RATIO_TOL, capped (the measure varies with the text)."""
    seed = _seed_weight(fam, obs.ratio)
    w = fam.clamp(FIXED_WEIGHT) if fam.variable else seed
    lay = _layout(obs.text, fam.fonts(w, fb_weight=seed), bitmaps=False)
    if fam.variable and abs(seed - w) >= ADV_STEP / 2:   # the seed weight's advances, the fixed weight's shapes
        ws = fam.clamp(round(seed / ADV_STEP) * ADV_STEP)
        lay = _reshaped(lay, advances(fam.face(ws), ws, set(obs.text) - lay.fonts.fb_chars))
    tops, bottoms, rows, parts = [], [], [], []
    for li, row in enumerate(lay.lines):
        lefts, rights = [], []
        for j, (ch, x, fb) in enumerate(row):
            g = lay.glyph(ch, fb)
            if not g.ink:
                continue
            sc = lay.fonts.fb_scale if fb else 1.0
            l, tp, r, bt = (v * sc for v in g.box)
            lefts.append((x + l, j))
            rights.append((x + r, j))
            if li == 0:   # the ink from the first line's top …
                tops.append(tp)
                parts += [(a * sc, b * sc) for a, b in g.parts]
            if li == len(lay.lines) - 1:   # … to the last line's bottom, less the observed pitches
                bottoms.append(bt)
        if lefts:
            rows.append((min(lefts), max(rights)))
    if not rows or not tops or not bottoms:
        return float("inf"), w, obs.h, 0.0, None
    n = len(lay.lines)
    cap_r = _cap_r(parts)
    if cap_r > 0 and obs.cap > 0:   # size from the cap height (a bolder face's dots and overshoots reach higher)
        s = obs.cap / obs.f * R / cap_r
    else:   # … else from the ink height
        h_line = obs.h - (n - 1) * obs.pitch
        s = (h_line if h_line > 0 else obs.h) * R / max(max(bottoms) - min(tops), 1e-6)
    widths = [(r[0] - l[0], r[1] - l[1]) for l, r in rows]
    d, rms_em = 0.0, None
    fits = _centres_fit(obs, lay, s, (-10.0, -5.0, 0.0, 5.0, 10.0))
    if fits:
        t, rms, _ = min(fits, key=lambda f: f[1])
        rms_em = rms / s
        d += rms_em
    else:
        base_w, nn = max(widths)
        t = float(np.clip((obs.w * R / s - base_w) / (nn * R) if nn else 0.0, *TRACK_SEARCH))
    w_pred = max((bw + nn * t * R) * s / R for bw, nn in widths)
    d += abs(math.log(max(obs.w, 1e-6) / max(w_pred, 1e-6)))
    d += max(0.0, TRACK_PLAUSIBLE[0] - t, t - TRACK_PLAUSIBLE[1])
    rr = _ratio_range(fam)
    if obs.ratio is not None and rr is not None and rr[0] > 0:   # relative, with slack: the measure varies by text
        d += min(RATIO_CAP, RATIO_W * max(0.0, rr[0] / obs.ratio - 1 - RATIO_TOL, obs.ratio / rr[1] - 1 - RATIO_TOL))
    return d, w, s, t, rms_em


def _reshaped(lay: _Layout, adv: dict[str, float]) -> _Layout:
    """The layout with other advances: every primary glyph's ink box and centroid stretched by its advance ratio."""
    glyphs = {}
    for ch, g in lay.pset.glyphs.items():
        a = adv.get(ch, g.adv)
        k = a / g.adv if g.adv > 1e-6 else 1.0
        glyphs[ch] = _Glyph(a, (g.box[0] * k, g.box[1], g.box[2] * k, g.box[3]), g.img, g.ox, g.oy, g.cx * k, g.cy,
                            g.parts, g.ink)
    pset = _GlyphSet(glyphs, lay.pset.kern)
    return _layout_with(lay.fonts, lay.text, pset, lay.fset)


def _coarse(obs: _Obs, fam: _Family, w: float, s: float, t: float) -> tuple[float, tuple]:
    """(soft IoU, (weight, size, tracking, whole runs, fallback scale)) of the family at the seed weight, its
    cap-height size and the tracking that follows (best of three shears; a few fallback scales when Hangul needs
    one) — also where its fit starts."""
    ft = _Fitter(obs, fam)
    shears = (-8.0, 0.0, 8.0)

    def one(t: float, s: float) -> tuple[float, float, float, bool]:
        lay = ft.layout(w)
        b = ft.base(lay, t, s)
        if b is None:
            return -1.0, s, t, False
        s = ft.size_from_cap(b)
        if obs.positional:   # positions (and a fallback's scale) follow the size
            scale = lay.fonts.fb_scale
            lay, t2 = ft.settle(w, s, 0.0, t)
            if abs(t2 - t) > 0.002 or lay.fonts.fb_scale != scale:
                t = t2
                b = ft.base(lay, t, s) or b
            best = (max(ft.score(b, s, k) for k in shears), t)
        else:
            best = (-1.0, t)   # no positions: a short tracking search around the width's estimate
            for tv in sorted({float(np.clip(t + d, *TRACK_SEARCH)) for d in COARSE_TRACK}):
                bb = ft.base(lay, tv, s)
                if bb is not None:
                    best = max(best, (max(ft.score(bb, s, k) for k in shears), tv))
        whole = False
        if abs(best[1]) <= LIG_T + 0.02 and lay.fset is None:   # untracked: whole runs, ligatures on
            bb = ft.base(lay, 0.0, s, whole=True)
            if bb is not None:
                v = max(ft.score(bb, s, k) for k in shears)
                if v > best[0]:
                    best, whole = (v, 0.0), True
        return best[0], s, best[1], whole

    if not fam.hangul_missing or obs.positional:   # the centres give a fallback's scale
        weights = [w]
        if fam.variable and any(_HANGUL.match(c) for c in obs.chars):   # Hangul strokes skew the ratio's seed
            weights += [fam.clamp(w - 200), fam.clamp(w + 200)]
        best = None
        for wx in dict.fromkeys(weights):
            w = wx
            ft.fb_scale = None
            sc, s2, t2, whole = one(t, s)
            if best is None or sc > best[0]:
                best = (sc, (w, s2, t2, whole, ft.fb_scale))
        return best
    best = None
    for fs in COARSE_FB:
        ft.fb_scale = fs
        sc, s2, t2, whole = one(t, s)
        if best is None or sc > best[0]:
            best = (sc, (w, s2, t2, whole, fs))
    return best


def _start(obs: _Obs, fam: _Family, w: float, s: float, t: float) -> tuple[float, tuple]:
    """Stage 2 of the prefilter and the fit's start: `_coarse` at the seed weight (variable families: rounded to
    STAGE2_STEP) from stage 1's size and tracking."""
    if fam.variable:
        w = fam.clamp(round(_seed_weight(fam, obs.ratio) / STAGE2_STEP) * STAGE2_STEP)
    return _coarse(obs, fam, w, s, t)


def _prefilter(obs: _Obs, registry: FontRegistry, k: int) -> list[tuple[str, _Family, tuple, float]]:
    """The k best (family, its _Family, (weight, size, tracking, whole runs, fallback scale) to start the fit
    from, coarse score), best first."""
    fams = []
    for name in _eligible(registry, obs.chars):
        try:
            fams.append((name, _Family(name, registry, obs.chars)))
        except Exception as e:   # an unreadable font drops out
            log.warning("font prefilter skipped %s: %s", name, describe(e))

    def stage1():
        out = []
        for name, fam in fams:
            try:
                out.append((*_stage1(obs, fam), name, fam))
            except Exception as e:
                log.warning("font prefilter skipped %s: %s", name, describe(e))
        return sorted(out, key=lambda x: x[0])

    first = stage1()
    for _ in range(2):   # Task 9's centres, then the pieces: dropped when no family places its glyphs on them
        best_rms = min((x[4] for x in first if x[4] is not None), default=None)
        if not obs.positional or (best_rms is not None and best_rms <= CENTRES_RMS_MAX):
            break
        log.info("font match: glyph %s unreliable (best RMS %s em); matching without them",
                 "centres" if obs.centres is not None else "pieces", best_rms)
        if obs.centres is not None:
            obs.centres = None
        else:
            obs.pieces = None
        first = stage1()
    first = [x[:4] + x[5:] for x in first]
    n_glyphs = len([c for c in obs.text if not c.isspace()])
    k1 = STAGE1_K if n_glyphs > SHORT_TEXT else 3 * STAGE1_K   # a few glyphs place a family less surely
    keep = first[:k1 if obs.positional else 3 * STAGE1_K]   # touching glyphs: the width tells less
    second = []
    for d, w, s, t, name, fam in keep:
        try:
            sc, state = _start(obs, fam, w, s, t)
            second.append((sc, name, fam, state))
        except Exception as e:
            log.warning("font prefilter skipped %s: %s", name, describe(e))
    second.sort(key=lambda x: -x[0])
    return [(name, fam, start, sc) for sc, name, fam, start in second[:k]]


# --- public API ----------------------------------------------------------------------------------------------

def prefilter(alpha, text: str, registry: FontRegistry, k: int = K_PREFILTER, *, stroke_ratio: float | None = None,
              centres=None) -> list[str]:
    """The k families (uploaded + bundled covering the text) to fit: stage 1 ranks every family by its glyph
    positions, ink width, tracking and stroke ratio against the observation (`_stage1`); the best STAGE1_K (all of
    them when the glyphs give no positions) are drawn coarsely (`_coarse`) and the k best soft IoUs kept."""
    return [name for name, _, _, _ in _prefilter(_observe(alpha, text, stroke_ratio, centres), registry, k)]


def fit_family(alpha, text: str, family: str, registry: FontRegistry, *, stroke_ratio: float | None = None,
               centres=None) -> FontFit:
    """Weight, size, tracking, shear and placement of `family` that best redraw `alpha` (the text's box)."""
    return _fit(_observe(alpha, text, stroke_ratio, centres), family, registry)


def match_font(alpha, text: str, registry: FontRegistry, *, k: int = 3, stroke_ratio: float | None = None,
               centres=None) -> tuple[list[FontFit], float]:
    """Top-k fits (best first) and a confidence (1.0 when the best scores ≥ 0.80 and leads by ≥ 0.02, else 0.5).
    `alpha`: the text's coverage in its box; `centres`: native x of every non-space glyph (Task 9)."""
    obs = _observe(alpha, text, stroke_ratio, centres)
    return _fit_kept(obs, _prefilter(obs, registry, K_PREFILTER), k)


def _fit_kept(obs: _Obs, kept: list, k: int = 3) -> tuple[list[FontFit], float]:
    """The prefilter's families fitted (two rounds of coordinate descent), the top k and the confidence."""
    searches = []
    for i, (name, fam, start, coarse) in enumerate(kept):
        if i >= MIN_FITS and coarse < kept[0][3] - PRUNE:   # far behind the best coarse render: not fitted
            continue
        try:
            search = _Search(obs, fam, start)
            search.round(0)
            searches.append(search)
        except Exception as e:   # one family failing never ends the match
            log.warning("font fit skipped %s: %s", name, describe(e))
    searches.sort(key=lambda x: -x.score)
    fits = []
    for i, search in enumerate(searches):
        if i < ROUND2_K:
            first = search.state()
            try:
                search.round(1)
            except Exception as e:   # the first round's fit stands
                log.warning("font fit round 2 skipped %s: %s", search.fam.family, describe(e))
                search.restore(first)
        try:
            fits.append(search.result())
        except Exception as e:
            log.warning("font fit skipped %s: %s", search.fam.family, describe(e))
    if not fits:
        raise LookupError("no font could be fitted")
    fits.sort(key=lambda f: -f.score)
    margin = fits[0].score - fits[1].score if len(fits) > 1 else 1.0
    conf = 1.0 if fits[0].score >= CONF_TOP and margin >= CONF_MARGIN else 0.5
    return fits[:k], conf


def fit_hangul_fallback(best: FontFit, alpha, text: str, registry: FontRegistry) -> tuple[str | None, int | None, float]:
    """(family, weight, scale) of the Hangul fallback for a best face that lacks the text's Hangul: the registry's
    choice for its category, then weight and scale refitted with the primary held at `best`. (None, None, 1.0) when
    no fallback is needed."""
    obs = _observe(alpha, text, None, None)
    fam = _Family(best.family, registry, obs.chars)
    if not fam.hangul_missing:
        return None, None, 1.0
    name, w0 = registry.hangul_fallback(best.family, _bucket(best.weight))
    fface = registry.face(name, w0)
    if fface is None:
        return None, None, 1.0
    ft = _Fitter(obs, fam)
    s, t, shear = best.size_px, best.tracking_em, best.shear_deg

    def score(wf: float, sc: float) -> float:
        ft.fb = (name, wf, sc)
        b = ft.base(ft.layout(best.weight), t, s)
        return ft.score(b, s, shear) if b is not None else -1.0

    def scale_search(wf: float) -> tuple[float, float]:
        cache: dict = {}
        best_sc = _golden(lambda x: score(wf, x / 1000.0), FALLBACK_SCALES[0] * 1000, FALLBACK_SCALES[1] * 1000, 8, cache)
        return best_sc / 1000.0, cache[best_sc]

    lo, hi = weight_range(fface)
    scale, _ = scale_search(float(w0))
    wf = float(w0)
    if hi > lo:
        cache: dict = {}
        wf = _golden(lambda x: score(x, scale), max(lo, w0 - 200), min(hi, w0 + 200), 6, cache)
    wf = float(min(max(_bucket(wf), lo), hi))
    scale, _ = scale_search(wf)
    return name, int(wf), round(scale, 3)


def _guess(fits: list[FontFit], conf: float, fb: tuple, registry: FontRegistry, prior) -> tuple["FontGuess", dict]:
    from ..ir.schema import FontGuess
    best = fits[0]
    face = registry.face(best.family, best.weight)
    base = prior.model_dump() if prior is not None else {}
    base.update(family_guess=best.family, weight=int(best.weight), size_px=float(best.size_px),
                candidates=[f.family for f in fits], scores=[float(f.score) for f in fits], confidence=float(conf),
                source=face.source if face is not None and face.source in ("bundled", "uploaded", "system") else "generic",
                postscript=face.postscript if face is not None and face.weight_range[0] == face.weight_range[1] else None,
                file=None,   # a variable face's name is its default instance's: none then
                fallback=fb[0], fallback_weight=fb[1], fallback_scale=float(fb[2]))
    layout = {"tracking_em": float(best.tracking_em), "shear_deg": float(best.shear_deg),
              "dx": float(best.dx), "dy": float(best.dy)}
    return FontGuess(**base), layout


def font_guess(alpha, text: str, registry: FontRegistry, *, stroke_ratio: float | None = None, centres=None,
               prior=None) -> tuple["FontGuess", dict]:
    """The analysis' FontGuess (best family, weight, size; top-3 candidates with scores; confidence; source; the
    PostScript name of a static face; the Hangul fallback when the best face lacks the text's Hangul) and the layout
    the style takes (tracking_em, shear_deg, dx, dy), from the matched alpha in the element's box. `prior`: the
    FontGuess the other fields are kept from."""
    fits, conf = match_font(alpha, text, registry, stroke_ratio=stroke_ratio, centres=centres)
    fb = (None, None, 1.0)
    if any(_HANGUL.match(c) for c in text or ""):
        try:
            fb = fit_hangul_fallback(fits[0], alpha, text, registry)
        except Exception as e:   # the renderers pick the registry's fallback on their own
            log.warning("Hangul fallback fit failed: %s", describe(e))
    return _guess(fits, conf, fb, registry, prior)


@dataclass
class TextJob:
    """One analysed text for `font_guesses`: its coverage in the element's box and Task 9's measures."""
    key: str
    alpha: np.ndarray
    text: str
    stroke_ratio: float | None = None
    centres: list | None = None
    prior: object = None              # the first guess (FontGuess) kept when the text is not matched


def _copy_guess(job: TextJob, guess, registry: FontRegistry) -> tuple | None:
    """A repeated text, on its own pixels: its prefilter must put the first match's family on top, then only its
    DUP_FITS best families are fitted (not K_PREFILTER, and no Hangul refit when that family stays first). Its
    candidates, scores and confidence are its own. None: match it in full."""
    obs = _observe(job.alpha, job.text, job.stroke_ratio, job.centres)
    kept = _prefilter(obs, registry, K_PREFILTER)
    if not kept or kept[0][0] != guess.family_guess:
        return None
    fits, conf = _fit_kept(obs, kept[:DUP_FITS])
    if fits[0].family != guess.family_guess:
        return None
    return _guess(fits, conf, (guess.fallback, guess.fallback_weight, guess.fallback_scale), registry, job.prior)


def font_guesses(jobs: list[TextJob], registry: FontRegistry, *, max_renders: int | None = None) -> dict:
    """A scene's texts matched one after another — largest cap height first, ties in the given order — until
    `max_renders` candidate renders: per key ("ok", FontGuess, layout), ("error", MATCH_FAILED) or ("skipped",
    WORK_CAP) — codes only: they reach stage data and report messages; exception details are logged, paths cut.
    Work is counted, never timed, so the output does not depend on the host. A text repeated in the scene
    (same string) is matched on its own pixels with less work when its prefilter agrees with the first match
    (`_copy_guess`), else in full."""
    from ..analyze.textstyle import _a01, cap_height

    def cap(job: TextJob) -> float:
        try:
            return float(cap_height(_a01(job.alpha), max(1, (job.text or "").count("\n") + 1)))
        except Exception:
            return 0.0

    max_renders = SCENE_RENDERS if max_renders is None else max_renders
    order = sorted(range(len(jobs)), key=lambda i: (-cap(jobs[i]), i))
    counter = [0]
    token = _RENDERS.set(counter)
    out: dict = {}
    first: dict = {}   # text -> FontGuess of its first match
    try:
        for i in order:
            job = jobs[i]
            if counter[0] >= max_renders:
                out[job.key] = ("skipped", WORK_CAP)
                continue
            key = (job.text or "").strip()
            if key in first:
                try:
                    res = _copy_guess(job, first[key], registry)
                    if res is not None:
                        out[job.key] = ("ok", *res)
                        continue
                except Exception as e:   # matched in full below
                    log.warning("font match of repeated %s: %s; matching it in full", job.key, describe(e))
            try:
                g, layout = font_guess(job.alpha, job.text, registry, prior=job.prior, stroke_ratio=job.stroke_ratio,
                                       centres=job.centres)
                first.setdefault(key, g)
                out[job.key] = ("ok", g, layout)
            except Exception as e:   # fail soft: the caller keeps the first guess; the detail stays in the log
                log.warning("font match failed for %s: %s", job.key, describe(e, trace=True))
                out[job.key] = ("error", MATCH_FAILED)
    finally:
        _RENDERS.reset(token)
        flush_cache()
    return out
