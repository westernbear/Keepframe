from __future__ import annotations

import base64
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import cv2
import numpy as np

from ..ir.colour import rgb8_to_hex
from ..ir.schema import Background, Element, FontGuess, Scene, TextStyle
from ..ir.synth import make_text_texture
from ..assets import AssetAPIError, validate_glb
from ..fonts.raster import embedded_face, natural_box, render_styled, resolve_fonts
from ..fonts.registry import FontRegistry
from ..log import get
from .retime import apply_timing
from .svgraster import rasterize_svg
from .textraster import measure as _measure, render_lines

log = get("keepframe.edit")
_DATA_URL = re.compile(r"^data:(?:image/[^;]+|model/gltf-binary|application/octet-stream);base64,(.+)$", re.S)


def measure_text(text: str, size_px: float, family: str = "sans-serif") -> tuple[int, int]:
    got = _measure(text, size_px, family)
    if got is not None:
        return got
    scale = max(0.2, size_px / 22.0)      # fallback: no fontconfig (Hershey, ASCII only)
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


def write_text_texture(path: Path, text: str, size_px: float, color: tuple[int, int, int], lines: list[str] | None = None,
                       family: str = "sans-serif", *, font: FontGuess | None = None, style: TextStyle | None = None,
                       fonts: FontRegistry | None = None, scene_dir: Path | None = None,
                       box: tuple[float, float] | None = None) -> tuple[int, int]:
    """With `font`: the styled raster the composer CSS matches (box-sized; natural box when None). Without it, or
    if that fails: today's measured texture."""
    if font is not None:
        try:
            img = render_styled(lines or [text], font.model_copy(update={"size_px": float(size_px)}), rgb8_to_hex(color),
                                style, registry=fonts, scene_dir=scene_dir, box=box)
            path.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(path), img[..., [2, 1, 0, 3]])
            h, w = img.shape[:2]
            return w, h
        except Exception as e:   # fail soft: a texture without the style beats no edit
            log.warning("styled text raster failed (%s); writing a plain texture", e)
    img = render_lines(lines or [text], size_px, color, family)
    if img is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(path), img)
        h, w = img.shape[:2]
        return w, h
    rows = lines or [text]
    if len(rows) == 1:
        return make_text_texture(path, rows[0], int(round(size_px)), color)
    sizes = [measure_text(row, size_px, family) for row in rows]
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


def _attachment_data(src: str | Path | bytes) -> bytes:
    if isinstance(src, bytes):
        return src
    try:
        if isinstance(src, Path):
            return src.read_bytes()
        if isinstance(src, str):
            m = _DATA_URL.match(src.strip())
            if m:
                return base64.b64decode(m.group(1), validate=True)
    except (ValueError, OSError) as exc:
        raise AssetAPIError("invalid_attachment") from exc
    raise AssetAPIError("invalid_attachment")


def save_attachment(src: str | Path | bytes, dest: Path) -> Path:
    data = _attachment_data(src)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest


def _is_svg(data: bytes) -> bool:
    try:
        return ET.fromstring(data).tag.rsplit("}", 1)[-1].lower() == "svg"
    except (ET.ParseError, LookupError, ValueError):
        return False


def _next_asset(scene_dir: Path, eid: str, suffix: str) -> Path:
    assets = scene_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        p = assets / f"{eid}.{suffix}{n}.png"
        if not p.exists():
            return p
        n += 1


def _next_model(scene_dir: Path, eid: str) -> Path:
    assets = scene_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    n = 1
    while (path := assets / f"{eid}.model{n}.glb").exists():
        n += 1
    return path


def apply_edit(scene: Scene, scene_dir: Path, items: list, choices: dict[str, str] | None, attachment: str | Path | bytes | None,
               *, fonts: FontRegistry | None = None) -> Scene:
    """`fonts`: the project's registry (uploads); bundled fonts only when None."""
    scene_dir = Path(scene_dir)
    fonts = fonts or FontRegistry()
    out = scene.model_copy(deep=True)
    choice_of = choices or {}
    for t in items:
        if t.property == "timing":
            apply_timing(out, [t], choice_of)
            if t.element is not None:
                out.element(t.element).provenance = "manual"
            continue
        if t.property == "background":
            out.background = Background(kind="color", value=t.value, confidence=1.0)
            continue
        el = out.element(t.element)
        if t.property == "text":
            _apply_text(el, scene_dir, t.value or "", choice_of.get("overflow") or choice_of.get(el.id), fonts)
        elif t.property == "font":
            if el.kind != "text" or not el.canonical.text:
                raise ValueError("font edit needs a text element")
            base = el.canonical.font or FontGuess()
            update = {k: v for k, v in (("family_guess", t.value), ("weight", t.weight)) if v}
            if t.value and t.value.casefold() != base.family_guess.casefold():
                # a new family: the analysed file, PostScript name and Hangul fallback belonged to the old one
                face = fonts.face(t.value)
                update.update(source=face.source if face else "generic", file=None, postscript=None, fallback=None,
                              fallback_weight=None, fallback_scale=1.0)
            el.canonical.font = base.model_copy(update=update)
            _apply_text(el, scene_dir, el.canonical.text, choice_of.get("overflow") or choice_of.get(el.id), fonts)
        elif t.property == "color":
            _apply_color(el, scene_dir, t.value or "#ffffff", fonts)
        elif t.property == "texture":
            if attachment is None:
                raise ValueError("texture edit needs an attachment")
            original = scene_dir / "assets" / f"{el.id}.png"
            crop = cv2.imread(str(original), cv2.IMREAD_UNCHANGED) if original.is_file() else None
            box_w, box_h = (crop.shape[1], crop.shape[0]) if crop is not None else (el.canonical.width, el.canonical.height)
            data = _attachment_data(attachment)
            if _is_svg(data):
                data = rasterize_svg(data, math.ceil(box_w), math.ceil(box_h))
            dest = _next_asset(scene_dir, el.id, "tex")
            save_attachment(data, dest)
            img = cv2.imread(str(dest), cv2.IMREAD_UNCHANGED)
            if img is None:
                raise AssetAPIError("invalid_attachment", "could not read attachment image")
            el.canonical.texture = f"assets/{dest.name}"
            h, w = img.shape[:2]
            scale = min(box_w / w, box_h / h)
            el.canonical.width, el.canonical.height = float(w * scale), float(h * scale)
        elif t.property == "model":
            if attachment is None:
                raise ValueError("3D edit needs a GLB attachment")
            try:
                data = validate_glb(_attachment_data(attachment))
            except UnicodeDecodeError as exc:
                raise AssetAPIError("invalid_glb") from exc
            dest = _next_model(scene_dir, el.id)
            dest.write_bytes(data)
            el.kind = "3d"
            el.pending_asset = None
            el.canonical.model = f"assets/{dest.name}"
        el.provenance = "manual"
    return out


def _styled(el: Element, fonts: FontRegistry, scene_dir: Path) -> bool:
    c = el.canonical
    return c.font is not None and (c.style is not None or embedded_face(c.font, fonts, scene_dir))


def _apply_styled_text(el: Element, scene_dir: Path, text: str, overflow: str | None, fonts: FontRegistry) -> bool:
    """Re-render styled text in its box: same line height, analysed fill/effects/tracking; the box widens (and
    gains lines) only when the new text needs it."""
    c = el.canonical
    font, style = c.font, c.style or TextStyle()
    lines = _split_wrap(text) if overflow == "wrap" else text.split("\n")
    resolved = resolve_fonts(font, text, fonts, scene_dir)
    if resolved is None:
        return False
    natural = lambda size: natural_box(lines, size, resolved, tracking_em=style.tracking_em, shear_deg=style.shear_deg, dx=style.dx)
    size = font.size_px
    if overflow == "shrink_font":
        while size > 8 and natural(size)[0] > c.width:
            size -= 2
        font = font.model_copy(update={"size_px": float(size)})
    need_w = float(natural(size)[0])
    width = need_w if overflow == "expand_box" or need_w > c.width else c.width
    height = float(round(c.height / max(1, (c.text or "").count("\n") + 1) * len(lines)))   # keeps the line height
    dest = _next_asset(scene_dir, el.id, "txt")
    write_text_texture(dest, text, size, _rgb(c.color or "#ffffff"), lines=lines, font=font, style=c.style, fonts=fonts,
                       scene_dir=scene_dir, box=(width, height))
    c.text = "\n".join(lines)   # the composer draws live text: keep the wrap it was rendered with
    c.font = font
    c.texture = f"assets/{dest.name}"
    c.width, c.height = width, height
    return True


def _apply_text(el: Element, scene_dir: Path, text: str, overflow: str | None, fonts: FontRegistry | None = None) -> None:
    fonts = fonts or FontRegistry()
    if _styled(el, fonts, scene_dir) and _apply_styled_text(el, scene_dir, text, overflow, fonts):
        return
    font = el.canonical.font or FontGuess()
    color = _rgb(el.canonical.color or "#ffffff")
    size = font.size_px
    lines = None
    if overflow == "wrap":
        lines = _split_wrap(text)
    elif overflow == "shrink_font":
        while size > 8 and measure_text(text, size, font.family_guess)[0] > el.canonical.width:
            size -= 2
        font = font.model_copy(update={"size_px": size})
    dest = _next_asset(scene_dir, el.id, "txt")
    w, h = write_text_texture(dest, text, font.size_px, color, lines=lines, family=font.family_guess)
    el.canonical.text = text
    el.canonical.font = font
    el.canonical.texture = f"assets/{dest.name}"
    if overflow == "expand_box" or overflow == "wrap" or w > el.canonical.width or h > el.canonical.height:
        el.canonical.width = float(w)
        el.canonical.height = float(h)


def _apply_color(el: Element, scene_dir: Path, hexs: str, fonts: FontRegistry | None = None) -> None:
    fonts = fonts or FontRegistry()
    el.canonical.color = hexs
    color = _rgb(hexs)
    c = el.canonical
    if el.kind == "text" and c.text and _styled(el, fonts, scene_dir):
        if c.style is not None and c.style.fill is not None:
            c.style = c.style.model_copy(update={"fill": None})   # a solid colour replaces the gradient
        dest = _next_asset(scene_dir, el.id, "txt")
        write_text_texture(dest, c.text, c.font.size_px, color, lines=c.text.split("\n"), font=c.font, style=c.style,
                           fonts=fonts, scene_dir=scene_dir, box=(c.width, c.height))
        c.texture = f"assets/{dest.name}"
        return
    if el.kind == "text" and el.canonical.text:
        dest = _next_asset(scene_dir, el.id, "txt")
        font = el.canonical.font or FontGuess()
        w, h = write_text_texture(dest, el.canonical.text, font.size_px, color, family=font.family_guess)
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
