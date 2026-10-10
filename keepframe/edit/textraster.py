from __future__ import annotations

import functools
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np

# ponytail: fontconfig lookup with a Hangul-capable face; Pillow cannot fall back per glyph.
_HANGUL = re.compile(r"[\uac00-\ud7a3\u1100-\u11ff\u3130-\u318f]")


def _fc(fmt: str, family: str, hangul: bool = False) -> str | None:
    if shutil.which("fc-match") is None:
        return None
    pattern = f"{family}:lang=ko" + (":charset=ac00" if hangul else "")
    try:
        out = subprocess.run(["fc-match", "-f", fmt, pattern], capture_output=True, text=True, timeout=5)
    except (subprocess.SubprocessError, OSError):
        return None
    value = out.stdout.strip()
    return value if out.returncode == 0 and value else None


@functools.lru_cache(maxsize=64)
def _font_match(family: str, hangul: bool) -> tuple[str, int]:
    value = _fc("%{file}\n%{index}", family, hangul)
    if value:
        parts = value.rsplit("\n", 1)
        if len(parts) == 2 and Path(parts[0]).is_file():
            try:
                return parts[0], int(parts[1])
            except ValueError:
                pass
    # Exceptions are not cached, so a transient lookup failure can recover.
    raise LookupError("fontconfig did not return a font file and face index")


def font_path(family: str = "sans-serif", hangul: bool = False) -> str | None:
    try:
        return _font_match(family, hangul)[0]
    except LookupError:
        return None


font_path.cache_clear = _font_match.cache_clear


@functools.lru_cache(maxsize=64)
def _families(family: str) -> tuple[str, ...]:
    value = _fc("%{family}", family)
    names = tuple(name.strip() for name in (value or "").split(",") if name.strip())
    if not names:
        raise LookupError("fontconfig did not return family names")
    return names


def resolve_families(family: str) -> tuple[str, ...]:
    try:
        return _families(family)
    except LookupError:
        return ()


def resolve_family(family: str) -> str | None:
    names = resolve_families(family)
    return names[0] if names else None


resolve_families.cache_clear = _families.cache_clear
resolve_family.cache_clear = _families.cache_clear


def _registry_font(family: str, size_px: float, hangul: bool):
    """A bundled face for `family` (fontconfig never sees the bundled set); None when there is none."""
    try:
        from ..fonts.raster import _mtime, _pil_font
        from ..fonts.registry import FontRegistry
        registry = FontRegistry()
        if family.casefold() not in {name.casefold() for name in registry.families()}:
            return None
        face = registry.face(family)
        if face is None or face.source == "system" or (hangul and not face.hangul):
            return None
        # uncached: callers here run outside the raster's FreeType lock
        return _pil_font.__wrapped__(str(face.path), face.index, float(max(1, round(size_px))), 400, _mtime(face.path))
    except Exception:   # an unreadable bundled file must not break the fontconfig path
        return None


def _font(family: str, size_px: float, hangul: bool = False):
    from PIL import ImageFont
    font = _registry_font(family, size_px, hangul)
    if font is not None:
        return font
    try:
        path, index = _font_match(family, hangul)
    except LookupError:
        return None
    return ImageFont.truetype(path, max(1, round(size_px)), index=index)


def _size(font, text: str) -> tuple[int, int]:
    x0, y0, x1, y1 = font.getbbox(text or " ")
    return int(x1 - min(0, x0)) + 4, int(y1 - min(0, y0)) + 4


def measure(text: str, size_px: float, family: str = "sans-serif") -> tuple[int, int] | None:
    font = _font(family, size_px, _HANGUL.search(text) is not None)
    if font is None:
        return None
    return _size(font, text)


def render_lines(lines: list[str], size_px: float, rgb: tuple[int, int, int], family: str = "sans-serif") -> np.ndarray | None:
    from PIL import Image, ImageDraw
    font = _font(family, size_px, any(_HANGUL.search(line) for line in lines))
    if font is None:
        return None
    sizes = [_size(font, line) for line in lines]
    img = Image.new("RGBA", (max(s[0] for s in sizes), sum(s[1] for s in sizes)), (0, 0, 0, 0))
    draw, y = ImageDraw.Draw(img), 0
    for line, (_, h) in zip(lines, sizes):
        x0, y0, _, _ = font.getbbox(line or " ")
        draw.text((2 - min(0, x0), y + 2 - min(0, y0)), line, font=font, fill=(*rgb, 255))
        y += h
    return np.array(img)[..., [2, 1, 0, 3]]   # RGBA -> BGRA for cv2.imwrite
