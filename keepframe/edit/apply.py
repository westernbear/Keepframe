from __future__ import annotations

import base64
import re
import shutil
from pathlib import Path

import cv2
import numpy as np

from ..ir.schema import Element, FontGuess, Scene
from ..ir.synth import make_text_texture

_DATA_URL = re.compile(r"^data:image/[^;]+;base64,(.+)$", re.S)


def measure_text(text: str, size_px: float) -> tuple[int, int]:
    scale = max(0.2, size_px / 22.0)
    thick = max(1, int(round(scale * 2)))
    (tw, th), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thick)
    return tw + 4, th + base + 4


def _rgb(hexs: str) -> tuple[int, int, int]:
    h = hexs.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _split_wrap(text: str) -> list[str]:
    mid = max(1, len(text) // 2)
    sp = text.rfind(" ", 0, mid + 1)
    if sp <= 0:
        sp = mid
    a, b = text[:sp].strip(), text[sp:].strip()
    return [p for p in (a, b) if p] or [text]


def write_text_texture(path: Path, text: str, size_px: float, color: tuple[int, int, int], lines: list[str] | None = None) -> tuple[int, int]:
    rows = lines or [text]
    if len(rows) == 1:
        return make_text_texture(path, rows[0], int(round(size_px)), color)
    sizes = [measure_text(row, size_px) for row in rows]
    w = max(s[0] for s in sizes)
    h = sum(s[1] for s in sizes)
    img = np.zeros((h, w, 4), np.uint8)
    y = 0
    scale = max(0.2, size_px / 22.0)
    thick = max(1, int(round(scale * 2)))
    bgr = (color[2], color[1], color[0], 255)
    for row, (_rw, rh) in zip(rows, sizes):
        (tw, th), base = cv2.getTextSize(row, cv2.FONT_HERSHEY_SIMPLEX, scale, thick)
        cv2.putText(img, row, (2, y + th + 2), cv2.FONT_HERSHEY_SIMPLEX, scale, bgr, thick, cv2.LINE_8)
        y += rh
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), img)
    return w, h


def save_attachment(src: str | Path | bytes, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(src, Path):
        shutil.copy2(src, dest)
        return dest
    if isinstance(src, bytes):
        dest.write_bytes(src)
        return dest
    m = _DATA_URL.match(src.strip())
    if m:
        dest.write_bytes(base64.b64decode(m.group(1)))
        return dest
    p = Path(src)
    if p.is_file():
        shutil.copy2(p, dest)
        return dest
    raise ValueError("attachment must be a data URL or image path")


def _next_asset(scene_dir: Path, eid: str, suffix: str) -> Path:
    assets = scene_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        p = assets / f"{eid}.{suffix}{n}.png"
        if not p.exists():
            return p
        n += 1


def apply_edit(scene: Scene, scene_dir: Path, items: list, choices: dict[str, str] | None, attachment: str | Path | bytes | None) -> Scene:
    scene_dir = Path(scene_dir)
    out = scene.model_copy(deep=True)
    choice_of = choices or {}
    for t in items:
        el = out.element(t.element)
        if t.property == "text":
            _apply_text(el, scene_dir, t.value or "", choice_of.get("overflow") or choice_of.get(el.id))
        elif t.property == "color":
            _apply_color(el, scene_dir, t.value or "#ffffff")
        elif t.property == "texture":
            if attachment is None:
                raise ValueError("texture edit needs an attachment")
            dest = _next_asset(scene_dir, el.id, "tex")
            save_attachment(attachment, dest)
            img = cv2.imread(str(dest), cv2.IMREAD_UNCHANGED)
            if img is None:
                raise ValueError("could not read attachment image")
            el.canonical.texture = f"assets/{dest.name}"
            el.canonical.height = float(img.shape[0])
            el.canonical.width = float(img.shape[1])
        el.provenance = "manual"
    return out


def _apply_text(el: Element, scene_dir: Path, text: str, overflow: str | None) -> None:
    font = el.canonical.font or FontGuess()
    color = _rgb(el.canonical.color or "#ffffff")
    size = font.size_px
    lines = None
    if overflow == "wrap":
        lines = _split_wrap(text)
    elif overflow == "shrink_font":
        while size > 8 and measure_text(text, size)[0] > el.canonical.width:
            size -= 2
        font = font.model_copy(update={"size_px": size})
    dest = _next_asset(scene_dir, el.id, "txt")
    w, h = write_text_texture(dest, text, font.size_px, color, lines=lines)
    el.canonical.text = text
    el.canonical.font = font
    el.canonical.texture = f"assets/{dest.name}"
    if overflow == "expand_box" or overflow == "wrap" or w > el.canonical.width or h > el.canonical.height:
        el.canonical.width = float(w)
        el.canonical.height = float(h)


def _apply_color(el: Element, scene_dir: Path, hexs: str) -> None:
    el.canonical.color = hexs
    color = _rgb(hexs)
    if el.kind == "text" and el.canonical.text:
        dest = _next_asset(scene_dir, el.id, "txt")
        font = el.canonical.font or FontGuess()
        w, h = write_text_texture(dest, el.canonical.text, font.size_px, color)
        el.canonical.texture = f"assets/{dest.name}"
        el.canonical.width = float(w)
        el.canonical.height = float(h)
        return
    src = scene_dir / (el.canonical.texture or "")
    if not src.is_file():
        return
    img = cv2.imread(str(src), cv2.IMREAD_UNCHANGED)
    if img is None:
        return
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA)
    elif img.shape[2] == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
    tint = np.array([color[2], color[1], color[0]], np.float32) / 255.0
    img = img.astype(np.float32)
    img[..., :3] *= tint
    dest = _next_asset(scene_dir, el.id, "tint")
    cv2.imwrite(str(dest), img.clip(0, 255).astype(np.uint8))
    el.canonical.texture = f"assets/{dest.name}"
