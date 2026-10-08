"""Composer CSS for styled text: the HTML side of `fonts/raster.py`, drawn the same way (browser-tested).

One element = a clipping wrapper (box-sized, fade as a mask) holding one absolutely positioned layer per effect
under the fill layer: shadows/glows (text in the effect colour, blurred σ = blur/2, offset, opacity), strokes
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
import tempfile
import threading
from pathlib import Path
from typing import Iterable

from ..ir.gradient import gradient_css
from ..ir.schema import Canonical, FontGuess, Gradient, GradientStop, Scene, TextStyle
from ..log import get
from .raster import (TextFonts, bounded_style, embedded_face, fade_stops, first_baseline, hex_rgb, resolve_fonts,
                     split_runs, text_size)
from .registry import FontFace, FontRegistry, safe_alias

log = get("keepframe.fonts")
BASIC_LATIN = frozenset(range(0x20, 0x7F))
_GENERIC = {"serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui"}
_SUBSET_LOCK = threading.Lock()   # one subset per key at a time (fontTools work is CPU-bound anyway)
SUBSET_VERSION = "1"


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
    return name if name.casefold() in _GENERIC else "'" + name.replace("\\", "").replace("'", "") + "'"


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
    th = math.radians(g.angle)
    s, c = abs(math.sin(th)), abs(math.cos(th))
    ratio = (w * s + h * c) / (w2 * s + h2 * c)
    return g.model_copy(update={"stops": [GradientStop(offset=0.5 + (st.offset - 0.5) * ratio, color=st.color) for st in g.stops]})


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
    wrapper = ["position:relative", "overflow:hidden"]
    if style.fade is not None:
        angle, stops = fade_stops(style.fade)
        mask = f"linear-gradient({_n(angle)}deg, " + ", ".join(f"rgba(0,0,0,{_n(a)}) {_n(o * 100)}%" for o, a in stops) + ")"
        wrapper += [f"-webkit-mask-image:{mask}", f"mask-image:{mask}"]
    return f'<span class="kf-text" style="{attr(wrapper)}">{"".join(layers)}<span style="{attr(fill)}">{text}</span></span>'


# --- @font-face ---------------------------------------------------------------------------------------------

def _source_sha(face: FontFace) -> str:
    return face.sha256 or hashlib.sha256(face.path.read_bytes()).hexdigest()


def subset_font(face: FontFace, codepoints: Iterable[int], *, wght: float | None = None,
                cache_dir: Path | str | None = None) -> bytes:
    """WOFF2 bytes of `face` cut to `codepoints` + Basic Latin, keeping the layout features browsers (and HarfBuzz in
    Pillow) apply by default; variable fonts pinned at `wght` when given. Cached by source hash, weight and
    codepoints."""
    cps = sorted(set(codepoints) | BASIC_LATIN)
    key = hashlib.sha256(f"{SUBSET_VERSION}|{_source_sha(face)}|{face.index}|{wght}|{cps}".encode()).hexdigest()[:32]
    root = cache_root(cache_dir)
    path = root / f"{key}.woff2"
    try:
        return path.read_bytes()
    except OSError:
        pass
    with _SUBSET_LOCK:
        try:
            return path.read_bytes()   # another thread made it meanwhile
        except OSError:
            pass
        data = _subset(face, cps, wght)
        _cache_write(root, path, data)
    return data


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
    except OSError as e:   # read-only home: embed without caching
        log.warning("font subset cache unavailable (%s)", e)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _font_data(face: FontFace, cps: set[int], wght: float | None, cache_dir) -> tuple[str, bytes] | None:
    try:
        return "font/woff2", subset_font(face, cps, wght=wght, cache_dir=cache_dir)
    except Exception as e:   # never embed a whole (possibly uploaded, megabytes) file: the browser falls back
        log.warning("font subset failed for %s (%s); not embedded", face.family, e)
        return None


def font_face_css(scene: Scene, scene_dir: Path | None, registry: FontRegistry | None = None, *,
                  cache_dir: Path | str | None = None) -> str:
    """@font-face rules for every bundled/uploaded face the scene's styled text uses ("" when none)."""
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
    rules = []
    for entry in sorted(used.values(), key=lambda e: (e["alias"], e["weight"])):
        got = _font_data(entry["face"], entry["cps"], entry["wght"], cache_dir)
        if got is None:
            continue
        mime, data = got
        rules.append(f"@font-face{{font-family:{entry['alias']};src:url(data:{mime};base64,{base64.b64encode(data).decode('ascii')});"
                     f"font-weight:{entry['weight']};font-style:normal;font-display:block{entry['extra']}}}")
    return "\n".join(rules)
