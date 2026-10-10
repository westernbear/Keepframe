"""Composer CSS for styled text: the HTML side of `fonts/raster.py`, drawn the same way (browser-tested).

One element = a wrapper at the text box holding one absolutely positioned layer per effect under the fill layer,
all clipped (fade as a mask) to the box, or, when effects reach past it (R41), to the raster's effect canvas (the
box grown by `effect_pad`): shadows/glows (text in the effect colour, blurred σ = blur/2, offset, opacity), strokes
(text and `-webkit-text-stroke: 2w` in the stroke colour, so the stroke covers dilate(α, w)), then the fill
(colour, or the gradient through `background-clip: text`). Every layer shares `text_css`'s layout
(geometricPrecision, LayoutUnit line height, letter-spacing in em, skewX about the first baseline).
Fonts are embedded as `@font-face` data URIs, subset with fontTools to the used codepoints + Basic Latin and
cached in `~/.cache/keepframe/font-subsets/` (`KEEPFRAME_FONT_CACHE` or `cache_dir` overrides).
"""
from __future__ import annotations

import base64
import hashlib
import html
import io
import math
import os
import re
import tempfile
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Iterable

from ..ir.gradient import gradient_css
from ..ir.schema import Canonical, FontGuess, Gradient, GradientStop, Scene, TextStyle
from ..log import get, scrub_paths
from .raster import (TextFonts, bounded_style, embedded_face, fade_stops, first_baseline, hex_rgb, resolve_fonts,
                     scene_font_file, split_runs, text_canvas, text_size)
from .registry import FontFace, FontRegistry, safe_alias

log = get("keepframe.fonts")
BASIC_LATIN = frozenset(range(0x20, 0x7F))
_GENERIC = {"serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui"}
SUBSET_VERSION = "1"
# Subsetting runs on a small pool so one slow font (fontTools decoding a large WOFF2 can take ~50 s) never holds
# a compose: compose waits at most SUBSET_WAIT_S for all its faces, then embeds what is ready; the rest keeps
# running and is cached for the next compose. A key subsets once at a time; a failed key is not retried for
# SUBSET_RETRY_S (R36).
SUBSET_WORKERS = 2
SUBSET_WAIT_S = 20.0
FINAL_WAIT_S = 300.0               # final renders wait this long and fail rather than fall back (R39)
SUBSET_RETRY_S = 600.0
MAX_PENDING = 32
MAX_SOURCE_BYTES = 20 * 2**20      # uploads are <= 20 MiB (Task 11); bundled files <= 5.9 MB
MAX_CODEPOINTS = 2048              # per face
MAX_CACHE_BYTES = 256 * 2**20      # oldest subsets evicted first


def _n(v: float) -> str:
    v = float(v) if math.isfinite(v) else 0.0
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _hex(value: str | None, default: str = "#000000") -> str:
    return "#{:02x}{:02x}{:02x}".format(*hex_rgb(value, default))


def cache_root(cache_dir: Path | str | None = None) -> Path:
    if cache_dir is not None:
        return Path(cache_dir)
    env = os.environ.get("KEEPFRAME_FONT_CACHE")
    return Path(env) if env else Path.home() / ".cache" / "keepframe" / "font-subsets"


def face_alias(face: FontFace) -> str:
    alias = safe_alias(face.family)
    if face.source == "uploaded":
        alias += "-" + (face.sha256 or hashlib.sha256(str(face.path).encode()).hexdigest())[:8]
    return alias


def fallback_alias(fonts: TextFonts) -> str:
    alias = f"{face_alias(fonts.fallback)}-fb{fonts.fallback_weight}"
    return alias if fonts.fallback_scale == 1 else f"{alias}-s{round(fonts.fallback_scale * 10000)}"


def _css_family(name: str) -> str:
    """A system family name as a quoted CSS string: letters, digits, spaces and hyphens only (the FontGuess rule)."""
    return name if name.casefold() in _GENERIC else "'" + re.sub(r"[^\w \-]", "", name) + "'"


def families(font: FontGuess, fonts: TextFonts) -> str:
    out = [face_alias(fonts.primary) if fonts.embedded else _css_family(font.family_guess)]
    if fonts.fallback is not None:
        out.append(fallback_alias(fonts) if fonts.fallback.source in ("bundled", "uploaded") else _css_family(fonts.fallback.family))
    return ",".join(out)


def _layout_css(font: FontGuess, style: TextStyle, box_wh: tuple[float, float], text: str, fonts: TextFonts) -> list[str]:
    lines = max(1, text.count("\n") + 1)
    line_h = round(float(box_wh[1]) / lines, 4)
    size = text_size(font.size_px, line_h)
    decl = ["position:absolute", f"left:{_n(style.dx)}px", f"top:{_n(style.dy)}px",
            f"font-family:{families(font, fonts)}", f"font-weight:{fonts.weight}", "font-style:normal",
            f"font-size:{_n(size)}px", f"line-height:{_n(line_h)}px", "font-synthesis:none", "font-kerning:normal",
            "font-optical-sizing:none", "text-rendering:geometricPrecision"]
    if style.tracking_em:
        decl.append(f"letter-spacing:{_n(style.tracking_em)}em")
    if style.shear_deg:
        decl += [f"transform:skewX({_n(-style.shear_deg)}deg)",
                 f"transform-origin:0 {first_baseline(fonts.primary, size, line_h)}px"]
    return decl


def _padded(g: Gradient, w: float, h: float, p: float) -> Gradient:
    """The same gradient on a (w+2p)×(h+2p) image centred on the box: equal inside, clamped outside (as numpy)."""
    w2, h2 = w + 2 * p, h + 2 * p
    if g.kind == "radial":
        r = g.radius * math.hypot(w, h) / 2
        return g.model_copy(update={"center": ((g.center[0] * w + p) / w2, (g.center[1] * h + p) / h2),
                                    "radius": r / (math.hypot(w2, h2) / 2)})
    ratio = _pad_ratio(g.angle, w, h, p)
    return g.model_copy(update={"stops": [GradientStop(offset=0.5 + (st.offset - 0.5) * ratio, color=st.color) for st in g.stops]})


def _pad_ratio(angle: float, w: float, h: float, p: float) -> float:
    """Gradient-line length of a w×h box over that of the box grown by p on every side (same angle)."""
    th = math.radians(angle)
    s, c = abs(math.sin(th)), abs(math.cos(th))
    return (w * s + h * c) / ((w + 2 * p) * s + (h + 2 * p) * c)


def _fade_mask(style: TextStyle, box_wh: tuple[float, float], pad: int) -> list[str]:
    """The fade as a CSS mask over the box grown by `pad` (equal inside the box, clamped outside, as the raster)."""
    if style.fade is None:
        return []
    angle, stops = fade_stops(style.fade)
    if pad:
        ratio = _pad_ratio(angle, float(box_wh[0]), float(box_wh[1]), pad)
        stops = [(0.5 + (o - 0.5) * ratio, a) for o, a in stops]
    mask = f"linear-gradient({_n(angle)}deg, " + ", ".join(f"rgba(0,0,0,{_n(a)}) {_n(o * 100)}%" for o, a in stops) + ")"
    return [f"-webkit-mask-image:{mask}", f"mask-image:{mask}"]


def _fill_css(color: str | None, style: TextStyle, box_wh: tuple[float, float]) -> list[str]:
    if style.fill is None:
        return [f"color:{_hex(color)}"]
    w, h = float(box_wh[0]), float(box_wh[1])
    p = max(w, h)
    return ["color:transparent", "-webkit-text-fill-color:transparent",
            f"background-image:{gradient_css(_padded(style.fill, w, h, p), w + 2 * p, h + 2 * p)}",
            f"background-size:{_n(w + 2 * p)}px {_n(h + 2 * p)}px",
            f"background-position:{_n(-style.dx - p)}px {_n(-style.dy - p)}px", "background-repeat:no-repeat",
            "-webkit-background-clip:text", "background-clip:text"]


def text_css(font: FontGuess, color: str | None, style: TextStyle | None, box_wh: tuple[float, float], *, text: str = "",
             registry: FontRegistry | None = None, scene_dir: Path | None = None, fonts: TextFonts | None = None) -> str:
    """Declarations of the fill layer's span (layout + paint)."""
    style = bounded_style(style, box_wh)
    fonts = fonts or resolve_fonts(font, text, registry, scene_dir)
    if fonts is None:
        raise LookupError(f"no font for {font.family_guess!r}")
    return ";".join(_layout_css(font, style, box_wh, text, fonts) + _fill_css(color, style, box_wh))


def styled(c: Canonical, registry: FontRegistry, scene_dir: Path | None) -> bool:
    """Text the composer draws with the styled path (legacy text keeps its byte-identical span)."""
    return c.text is not None and c.font is not None and (c.style is not None or embedded_face(c.font, registry, scene_dir))


def text_html(c: Canonical, *, registry: FontRegistry | None = None, scene_dir: Path | None = None) -> str | None:
    """Layered markup for a styled text element; None for legacy text or when no font resolves."""
    registry = registry or FontRegistry()
    if not styled(c, registry, scene_dir):
        return None
    fonts = resolve_fonts(c.font, c.text, registry, scene_dir)
    if fonts is None:
        return None
    box = (c.width, c.height)
    style = bounded_style(c.style, box)
    base = ";".join(_layout_css(c.font, style, box, c.text, fonts))
    text = html.escape(c.text)
    attr = lambda decl: html.escape(";".join(decl) if not isinstance(decl, str) else decl, quote=True)
    layers = []
    for e in style.effects:
        if e.kind not in ("shadow", "glow"):
            continue
        outer = ["position:absolute", f"left:{_n(e.dx)}px", f"top:{_n(e.dy)}px"]
        if e.opacity < 1:
            outer.append(f"opacity:{_n(max(0.0, e.opacity))}")
        if e.blur > 0:
            outer.append(f"filter:blur({_n(e.blur / 2)}px)")
        layers.append(f'<span aria-hidden="true" style="{attr(outer)}"><span style="{attr(base + ";color:" + _hex(e.color))}">{text}</span></span>')
    for e in style.effects:
        if e.kind != "stroke" or not e.width > 0:
            continue
        outer = ["position:absolute", "left:0", "top:0"] + ([f"opacity:{_n(max(0.0, e.opacity))}"] if e.opacity < 1 else [])
        col = _hex(e.color)
        inner = f"{base};color:{col};-webkit-text-stroke:{_n(2 * e.width)}px {col}"
        layers.append(f'<span aria-hidden="true" style="{attr(outer)}"><span style="{attr(inner)}">{text}</span></span>')
    fill = base + ";" + ";".join(_fill_css(c.color, style, box))
    inner = f'{"".join(layers)}<span style="{attr(fill)}">{text}</span>'
    pad = text_canvas(box, style)[2]
    if not pad:   # no effects: the box clips, as before
        return f'<span class="kf-text" style="{attr(["position:relative", "overflow:hidden", *_fade_mask(style, box, 0)])}">{inner}</span>'
    # R41: effects reach past the box; every layer is clipped to the raster's effect canvas (the box + pad)
    w, h = float(box[0]), float(box[1])
    clip = ["position:absolute", f"left:{-pad}px", f"top:{-pad}px", f"width:{_n(w + 2 * pad)}px",
            f"height:{_n(h + 2 * pad)}px", "overflow:hidden", *_fade_mask(style, box, pad)]
    frame = ["position:absolute", f"left:{pad}px", f"top:{pad}px", f"width:{_n(w)}px", f"height:{_n(h)}px"]
    return (f'<span class="kf-text" style="position:relative;overflow:visible"><span style="{attr(clip)}">'
            f'<span style="{attr(frame)}">{inner}</span></span></span>')


# --- @font-face ---------------------------------------------------------------------------------------------

def _source_sha(face: FontFace) -> str:
    return face.sha256 or hashlib.sha256(face.path.read_bytes()).hexdigest()


class SubsetUnavailable(RuntimeError):
    """No subset now (too large, failed recently, busy or still running): the face is not embedded."""


class FontEmbedError(ValueError):
    """A final render's face could not be embedded: the render stops instead of baking a fallback font (R39)."""


_STATE = threading.Lock()
_INFLIGHT: dict[str, Future] = {}
_FAILED: OrderedDict[str, float] = OrderedDict()
_POOL: ThreadPoolExecutor | None = None
_EVICT = threading.Lock()


def _pool() -> ThreadPoolExecutor:
    global _POOL
    if _POOL is None:
        _POOL = ThreadPoolExecutor(SUBSET_WORKERS, thread_name_prefix="kf-subset")
    return _POOL


def _request(face: FontFace, codepoints: Iterable[int], wght: float | None, cache_dir) -> bytes | Future:
    """Cached bytes, or the (possibly shared) future of the subset job for this key."""
    if face.path.stat().st_size > MAX_SOURCE_BYTES:
        raise SubsetUnavailable(f"{face.path.name} is over {MAX_SOURCE_BYTES >> 20} MiB")
    cps = sorted(set(codepoints) | BASIC_LATIN)
    if len(cps) > MAX_CODEPOINTS:
        log.warning("font %s: %d codepoints, subset keeps %d", face.family, len(cps), MAX_CODEPOINTS)
        cps = cps[:MAX_CODEPOINTS]
    key = hashlib.sha256(f"{SUBSET_VERSION}|{_source_sha(face)}|{face.index}|{wght}|{cps}".encode()).hexdigest()[:32]
    root = cache_root(cache_dir)
    path = root / f"{key}.woff2"
    try:
        data = path.read_bytes()
        os.utime(path)   # recently used: last to be evicted
        return data
    except OSError:
        pass
    job = str(path)
    with _STATE:
        failed = _FAILED.get(job)
        if failed is not None and time.monotonic() - failed < SUBSET_RETRY_S:
            raise SubsetUnavailable("failed recently")
        future = _INFLIGHT.get(job)
        if future is None:
            if len(_INFLIGHT) >= MAX_PENDING:
                raise SubsetUnavailable("subset queue full")
            future = _INFLIGHT[job] = _pool().submit(_job, job, face, cps, wght, root, path)
    return future


def _job(job: str, face: FontFace, cps: list[int], wght: float | None, root: Path, path: Path) -> bytes:
    try:
        data = _subset(face, cps, wght)
        _cache_write(root, path, data)
        return data
    except Exception:
        with _STATE:
            _FAILED[job] = time.monotonic()
            while len(_FAILED) > 256:
                _FAILED.popitem(last=False)
        raise
    finally:
        with _STATE:
            _INFLIGHT.pop(job, None)


def _wait(got: bytes | Future, timeout: float) -> bytes:
    if isinstance(got, bytes):
        return got
    try:
        return got.result(timeout=max(0.0, timeout))
    except FutureTimeout:
        raise SubsetUnavailable("still subsetting") from None


def subset_font(face: FontFace, codepoints: Iterable[int], *, wght: float | None = None,
                cache_dir: Path | str | None = None, wait: float | None = None) -> bytes:
    """WOFF2 bytes of `face` cut to `codepoints` + Basic Latin, keeping the layout features browsers (and HarfBuzz in
    Pillow) apply by default; variable fonts pinned at `wght` when given. Cached by source hash, weight and
    codepoints; waits at most `wait` (SUBSET_WAIT_S) seconds, else raises SubsetUnavailable."""
    return _wait(_request(face, codepoints, wght, cache_dir), SUBSET_WAIT_S if wait is None else wait)


def _subset(face: FontFace, cps: list[int], wght: float | None) -> bytes:
    import logging
    from fontTools import subset
    from fontTools.ttLib import TTFont
    from .sfnt import sfnt_bytes
    for name in ("fontTools.subset", "fontTools.varLib"):   # both log every table at INFO
        logging.getLogger(name).setLevel(logging.WARNING)
    raw = sfnt_bytes(face.path, face.index) if face.path.suffix.lower() == ".woff2" else None
    opts = subset.Options()
    opts.notdef_outline = True
    opts.hinting = False
    opts.legacy_kern = True
    try:
        tt = TTFont(io.BytesIO(raw)) if raw else TTFont(str(face.path), fontNumber=face.index)
        sub = subset.Subsetter(opts)
        sub.populate(unicodes=cps)
        sub.subset(tt)
    except Exception:
        if not raw:
            raise
        tt = TTFont(str(face.path), fontNumber=face.index)   # FreeType's tables did not subset: fontTools decodes
        sub = subset.Subsetter(opts)
        sub.populate(unicodes=cps)
        sub.subset(tt)
    if wght is not None and "fvar" in tt:
        from fontTools.varLib import instancer
        limits = {a.axisTag: (float(wght) if a.axisTag == "wght" else None) for a in tt["fvar"].axes}
        tt = instancer.instantiateVariableFont(tt, limits)
    tt.flavor = "woff2"
    buf = io.BytesIO()
    tt.save(buf)
    return buf.getvalue()


def _cache_write(root: Path, path: Path, data: bytes) -> None:
    tmp = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=root, prefix=f".{path.stem}.", suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        tmp = None
        _evict(root)
    except OSError as e:   # read-only home: embed without caching
        log.warning("font subset cache unavailable (%s)", e)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _evict(root: Path) -> None:
    """Keep the cache under MAX_CACHE_BYTES, least recently used (mtime) first, down to 90 %."""
    with _EVICT:
        files = []
        for p in root.glob("*.woff2"):
            try:
                st = p.stat()
            except OSError:
                continue
            files.append((st.st_mtime_ns, st.st_size, p))
        total = sum(size for _, size, _ in files)
        if total <= MAX_CACHE_BYTES:
            return
        for _, size, p in sorted(files)[:-1]:   # never the file just written
            try:
                p.unlink()
            except OSError:
                continue
            total -= size
            if total <= MAX_CACHE_BYTES * 0.9:
                break


def uploaded_font_issues(scene: Scene, scene_dir: Path | None, registry: FontRegistry | None = None) -> list[dict]:
    """Text in an uploaded family that a composition cannot draw as analysed (R46/R47): `font_substituted` (no
    uploaded face for the family any more; `used` names what draws it instead) or, when the project's registry still
    has the family, `font_file_missing` (its FontGuess.file is gone, e.g. a project unpacked from a ZIP)."""
    registry = registry or FontRegistry()
    out = []
    for el in scene.elements:
        c = el.canonical
        f = c.font
        if el.kind != "text" or f is None or f.source != "uploaded" or not c.text:
            continue
        fonts = resolve_fonts(f, c.text, registry, scene_dir) if styled(c, registry, scene_dir) else None
        if fonts is None or fonts.primary.source != "uploaded":
            out.append({"kind": "font_substituted", "element": el.id, "family": f.family_guess,
                        "used": fonts.primary.family if fonts is not None else "sans-serif"})
        elif f.file and scene_font_file(f, scene_dir) is None:
            out.append({"kind": "font_file_missing", "element": el.id, "family": f.family_guess})
    return out


def font_face_css(scene: Scene, scene_dir: Path | None, registry: FontRegistry | None = None, *,
                  cache_dir: Path | str | None = None, wait: float | None = None, strict: bool = False) -> str:
    """@font-face rules for every bundled/uploaded face the scene's styled text uses ("" when none), waiting at most
    `wait` (SUBSET_WAIT_S) for all of them; `strict` (final renders) raises FontEmbedError for a face not embedded."""
    registry = registry or FontRegistry()
    used: dict[tuple, dict] = {}
    for el in scene.elements:
        c = el.canonical
        if el.kind != "text" or not styled(c, registry, scene_dir):
            continue
        fonts = resolve_fonts(c.font, c.text, registry, scene_dir)
        if fonts is None:
            continue
        if fonts.embedded:   # always declared: Blink takes line metrics from the first family
            p = fonts.primary
            lo, hi = p.weight_range
            used.setdefault(("p", str(p.path), p.index), {"face": p, "alias": face_alias(p), "wght": None,
                                                          "weight": f"{lo} {hi}" if lo != hi else f"{lo}",
                                                          "extra": "", "cps": set()})
        for line in c.text.split("\n"):
            for run, fb in split_runs(line, fonts):
                face = fonts.fallback if fb else fonts.primary
                if face.source not in ("bundled", "uploaded"):
                    continue
                if fb:
                    key = ("f", str(face.path), face.index, fonts.fallback_weight, fonts.fallback_scale)
                    entry = used.setdefault(key, {"face": face, "alias": fallback_alias(fonts), "wght": fonts.fallback_weight,
                                                  "weight": "1 1000", "extra": f";size-adjust:{_n(fonts.fallback_scale * 100)}%",
                                                  "cps": set()})
                else:
                    entry = used[("p", str(face.path), face.index)]
                entry["cps"].update(ord(ch) for ch in run)
    entries = sorted(used.values(), key=lambda e: (e["alias"], e["weight"]))
    requests = []
    for entry in entries:   # start every job first, then wait once for all of them
        try:
            requests.append(_request(entry["face"], entry["cps"], entry["wght"], cache_dir))
        except Exception as e:
            requests.append(e)
    deadline = time.monotonic() + (SUBSET_WAIT_S if wait is None else wait)
    rules = []
    for entry, got in zip(entries, requests):
        try:
            if isinstance(got, Exception):
                raise got
            data = _wait(got, deadline - time.monotonic())
        except Exception as e:   # never embed a whole (possibly uploaded, megabytes) file: the browser falls back
            log.warning("font %s not embedded (%s)", entry["face"].family, scrub_paths(f"{type(e).__name__}: {e}"))
            if strict:   # the client sees the family only; the detail stays in the log above
                raise FontEmbedError(f"font {entry['face'].family} could not be embedded for the final render; "
                                     "nothing was rendered") from e
            continue
        rules.append(f"@font-face{{font-family:{entry['alias']};src:url(data:font/woff2;base64,{base64.b64encode(data).decode('ascii')});"
                     f"font-weight:{entry['weight']};font-style:normal;font-display:block{entry['extra']}}}")
    return "\n".join(rules)
