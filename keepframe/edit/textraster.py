from __future__ import annotations

import functools
import shutil
import subprocess
from pathlib import Path

import numpy as np

# ponytail: fontconfig lookup; same resolver Chromium uses on Linux, so texture and <span> agree on the face.


def _fc(fmt: str, family: str) -> str | None:
    if shutil.which("fc-match") is None:
        return None
    out = subprocess.run(["fc-match", "-f", fmt, f"{family}:lang=ko"], capture_output=True, text=True, timeout=5)
    value = out.stdout.strip()
    return value if out.returncode == 0 and value else None


@functools.lru_cache(maxsize=64)
def font_path(family: str = "sans-serif") -> str | None:
    path = _fc("%{file}", family)
    return path if path and Path(path).is_file() else None


@functools.lru_cache(maxsize=64)
def resolve_family(family: str) -> str | None:
    name = _fc("%{family[0]}", family)
    return name.split(",")[0] if name else None


def _font(family: str, size_px: float):
    from PIL import ImageFont
    path = font_path(family)
    return ImageFont.truetype(path, max(1, round(size_px))) if path else None


def measure(text: str, size_px: float, family: str = "sans-serif") -> tuple[int, int] | None:
    font = _font(family, size_px)
    if font is None:
        return None
    x0, y0, x1, y1 = font.getbbox(text or " ")
    return int(x1 - min(0, x0)) + 4, int(y1 - min(0, y0)) + 4


def render_lines(lines: list[str], size_px: float, rgb: tuple[int, int, int], family: str = "sans-serif") -> np.ndarray | None:
    from PIL import Image, ImageDraw
    font = _font(family, size_px)
    if font is None:
        return None
    sizes = [measure(line, size_px, family) for line in lines]
    img = Image.new("RGBA", (max(s[0] for s in sizes), sum(s[1] for s in sizes)), (0, 0, 0, 0))
    draw, y = ImageDraw.Draw(img), 0
    for line, (_, h) in zip(lines, sizes):
        x0, y0, _, _ = font.getbbox(line or " ")
        draw.text((2 - min(0, x0), y + 2 - min(0, y0)), line, font=font, fill=(*rgb, 255))
        y += h
    return np.array(img)[..., [2, 1, 0, 3]]   # RGBA -> BGRA for cv2.imwrite
