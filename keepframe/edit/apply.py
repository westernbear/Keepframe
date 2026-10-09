from __future__ import annotations

import base64
import io
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
from ..fonts.raster import _HANGUL, MAX_TEXT_PX, cmap, embedded_face, natural_box, render_styled, resolve_fonts, text_size
from ..fonts.registry import FontRegistry
from ..fonts.upload import pin_face
from ..log import get, scrub_paths
from .retime import apply_timing
from .svgraster import rasterize_svg
from .tint import tint_background
from .textraster import measure as _measure, render_lines

log = get("keepframe.edit")
MAX_BACKGROUND_PX = 1 << 26   # pixels of an attached background picture
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


def _shrink(size: float, fits) -> float:
    """Where `while size > 8 and not fits(size): size -= 2` stops, found by bisection (O(log) layouts) and
    starting no higher than MAX_TEXT_PX, the largest size the raster draws (scene sizes are untrusted)."""
    s0 = min(float(size), MAX_TEXT_PX) if math.isfinite(size) else MAX_TEXT_PX
    if s0 <= 8 or fits(s0):
        return s0
    lo, hi = 0, math.ceil((s0 - 8) / 2)      # s0 - 2*lo does not fit; s0 - 2*hi <= 8 ends the walk
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if fits(s0 - 2 * mid):
            hi = mid
        else:
            lo = mid
    return s0 - 2 * hi


def _split_wrap(text: str) -> list[str]:
    mid = max(1, len(text) // 2)
    sp = text.rfind(" ", 0, mid + 1)
    if sp <= 0:
        sp = mid
    a, b = text[:sp].strip(), text[sp:].strip()
    return [p for p in (a, b) if p] or [text]


def edit_size(size_px: float, line_h: float | None = None) -> float:
    """A font size an edit may rasterise (R40): `text_size`'s bounds (≤ MAX_TEXT_PX and 8× the line box);
    a non-positive or non-finite size draws at the default 32 px (within the same bounds)."""
    try:
        return text_size(size_px, line_h)
    except ValueError:
        return text_size(32.0, line_h)


def write_text_texture(path: Path, text: str, size_px: float, color: tuple[int, int, int], lines: list[str] | None = None,
                       family: str = "sans-serif", *, font: FontGuess | None = None, style: TextStyle | None = None,
                       fonts: FontRegistry | None = None, scene_dir: Path | None = None,
                       box: tuple[float, float] | None = None, info: dict | None = None) -> tuple[int, int]:
    """With `font`: the styled raster the composer CSS matches, box-sized (natural box when None) with its effect
    canvas around it (`info["pad"]`, R41). Without it, or if that fails: today's measured texture (pad 0).
    Every size is clamped first (R40)."""
    rows = lines or [text]
    line_h = box[1] / len(rows) if box is not None and box[1] > 0 else None
    size_px = edit_size(size_px, line_h)
    if info is not None:
        info["pad"] = 0
    if font is not None:
        try:
            img, pad = render_styled(rows, font.model_copy(update={"size_px": float(size_px)}), rgb8_to_hex(color),
                                     style, registry=fonts, scene_dir=scene_dir, box=box, padded=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(path), img[..., [2, 1, 0, 3]])
            if info is not None:
                info["pad"] = pad
            h, w = img.shape[:2]
            return w - 2 * pad, h - 2 * pad
        except Exception as e:   # fail soft: a texture without the style beats no edit
            log.warning("styled text raster failed (%s); writing a plain texture", scrub_paths(e))
    img = render_lines(rows, size_px, color, family)
    if img is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(path), img)
        h, w = img.shape[:2]
        return w, h
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


def _background_image(scene_dir: Path, attachment) -> str:
    """The attached PNG as a new flat asset `assets/background.img<n>.png` (never an existing name); refused
    (`invalid_attachment`) unless it decodes as a PNG within MAX_BACKGROUND_PX."""
    from PIL import Image
    if attachment is None:
        raise ValueError("background image edit needs an attachment")
    data = _attachment_data(attachment)
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AssetAPIError("invalid_attachment")
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.format != "PNG" or im.size[0] * im.size[1] > MAX_BACKGROUND_PX:
                raise AssetAPIError("invalid_attachment")
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise AssetAPIError("invalid_attachment") from exc
    if cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR) is None:
        raise AssetAPIError("invalid_attachment")
    dest = _next_asset(scene_dir, "background", "img")
    with open(dest, "xb") as f:
        f.write(data)
    return f"assets/{dest.name}"


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
            if t.value == "attachment":   # a picture (e.g. one the agent made) becomes the background
                out.background = Background(kind="image", value=_background_image(scene_dir, attachment), confidence=1.0)
            elif t.mode == "tint" and out.background.kind != "color":   # chosen by the agent, never by Keepframe
                out.background = tint_background(out.background, scene_dir, t.value, size=out.size)
            else:
                out.background = Background(kind="color", value=t.value, confidence=1.0)
            continue
        el = out.element(t.element)
        if t.property == "text":
            _apply_text(el, scene_dir, t.value or "", choice_of.get("overflow") or choice_of.get(el.id), fonts, retext=True)
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
            family = update.get("family_guess", base.family_guess)
            face = fonts.face(family, update.get("weight", base.weight))
            if face is not None and face.source == "uploaded":
                # the uploaded face for this family and weight, copied into the scene (plans pin FontGuess.file)
                try:
                    pinned = pin_face(scene_dir, face)
                except (OSError, ValueError) as e:   # the registry still resolves the family
                    log.warning("uploaded font %s not copied into the scene: %s", family, scrub_paths(e))
                    pinned = None
                update.update(source="uploaded", file=pinned,
                              postscript=face.postscript if face.weight_range[0] == face.weight_range[1] else None)
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
            el.canonical.texture_pad = 0.0   # an attachment covers the box exactly
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
            el.canonical.texture_pad = 0.0
        if el.canonical.video and t.property in ("color", "texture", "model"):
            el.canonical.video = None   # the edited still (or model) replaces the clip: video is never recoloured (R55)
        el.provenance = "manual"
    return out


def _styled(el: Element, fonts: FontRegistry, scene_dir: Path) -> bool:
    """Text an edit draws with the styled raster: analysed style or an uploaded/bundled face (the composer draws
    those the same way)."""
    c = el.canonical
    return c.font is not None and (c.style is not None or embedded_face(c.font, fonts, scene_dir))


def _brings_hangul(el: Element, text: str, fonts: FontRegistry) -> bool:
    """A text edit that brings Hangul into a legacy title whose named face (on this machine) lacks it: the bundled
    Hangul fallback draws it (ahead of system fonts), so the title moves to the styled path. Colour and font edits
    never move a title, and a family this machine does not have is left to the browser, as before."""
    c = el.canonical
    if c.font is None or not _HANGUL.search(text) or _HANGUL.search(c.text or ""):
        return False
    face = fonts.face(c.font.family_guess, c.font.weight)
    if face is None:
        return False
    covered = cmap(face)
    return any(ord(ch) not in covered for ch in text if _HANGUL.match(ch))


def _line_h(c) -> float:
    return c.height / max(1, (c.text or "").count("\n") + 1)


def text_width(el: Element, text: str, font: FontGuess | None = None, *, fonts: FontRegistry | None = None,
               scene_dir: Path | None = None) -> float:
    """Width `text` takes as an edit would draw it (`font` replaces the element's for a font edit; without it the
    edit is a text edit): the styled layout (weight, tracking, shear, dx, fallback) or, for legacy text, the
    measured texture."""
    fonts = fonts or FontRegistry()
    c = el.canonical
    retext = font is None
    font = font or c.font or FontGuess()
    probe = el.model_copy(update={"canonical": c.model_copy(update={"font": font})})
    lines = text.split("\n")
    size = edit_size(font.size_px, _line_h(c))   # R40: measuring rasterises too
    if _styled(probe, fonts, scene_dir) or (retext and _brings_hangul(probe, text, fonts)):
        resolved = resolve_fonts(font, text, fonts, scene_dir)
        if resolved is not None:
            st = c.style or TextStyle()
            return float(natural_box(lines, size, resolved, tracking_em=st.tracking_em, shear_deg=st.shear_deg, dx=st.dx)[0])
    return float(max(measure_text(line, size, font.family_guess)[0] for line in lines))


def _write_styled(el: Element, scene_dir: Path, lines: list[str], font: FontGuess, color: tuple[int, int, int],
                  fonts: FontRegistry, box: tuple[float, float]) -> None:
    """Writes the element's styled texture and records it (texture, padding)."""
    c = el.canonical
    dest = _next_asset(scene_dir, el.id, "txt")
    info: dict = {}
    write_text_texture(dest, "\n".join(lines), font.size_px, color, lines=lines, family=font.family_guess, font=font,
                       style=c.style, fonts=fonts, scene_dir=scene_dir, box=box, info=info)
    c.texture = f"assets/{dest.name}"
    c.texture_pad = float(info.get("pad", 0))


def _apply_styled_text(el: Element, scene_dir: Path, text: str, overflow: str | None, fonts: FontRegistry) -> bool:
    """Re-render styled text in its box: same line height, analysed fill/effects/tracking; the box widens (and
    gains lines) only when the new text needs it. Effects past the box stay in the texture's padding (R41)."""
    c = el.canonical
    font, style = c.font, c.style or TextStyle()
    lines = _split_wrap(text) if overflow == "wrap" else text.split("\n")
    resolved = resolve_fonts(font, text, fonts, scene_dir)
    if resolved is None:
        return False
    natural = lambda size: natural_box(lines, size, resolved, tracking_em=style.tracking_em, shear_deg=style.shear_deg, dx=style.dx)
    size = edit_size(font.size_px, _line_h(c))   # R40: scene sizes are untrusted
    if overflow == "shrink_font":
        size = _shrink(size, lambda s: natural(s)[0] <= c.width)
    if size != font.size_px:
        font = font.model_copy(update={"size_px": float(size)})
    need_w = float(natural(size)[0])
    width = need_w if overflow == "expand_box" or need_w > c.width else c.width
    height = float(round(_line_h(c) * len(lines)))   # keeps the line height
    c.font = font
    _write_styled(el, scene_dir, lines, font, _rgb(c.color or "#ffffff"), fonts, (width, height))
    c.text = "\n".join(lines)   # the composer draws live text: keep the wrap it was rendered with
    c.width, c.height = width, height
    return True


def _apply_text(el: Element, scene_dir: Path, text: str, overflow: str | None, fonts: FontRegistry | None = None,
                retext: bool = False) -> None:
    """Fonts: uploaded > bundled > the bundled Hangul fallback > system (fontconfig) > Hershey; the analysed
    colour, style, effects and tracks are kept. `retext`: a text edit (only those may move a legacy title to the
    styled path, `_brings_hangul`)."""
    fonts = fonts or FontRegistry()
    moved = retext and not _styled(el, fonts, scene_dir) and _brings_hangul(el, text, fonts)
    if (moved or _styled(el, fonts, scene_dir)) and _apply_styled_text(el, scene_dir, text, overflow, fonts):
        if moved:   # the composer draws it the styled way too (same faces, fallback included)
            el.canonical.style = TextStyle()
        return
    font = el.canonical.font or FontGuess()
    color = _rgb(el.canonical.color or "#ffffff")
    size = edit_size(font.size_px, _line_h(el.canonical))   # R40
    lines = None
    if overflow == "wrap":
        lines = _split_wrap(text)
    elif overflow == "shrink_font":
        size = _shrink(size, lambda s: measure_text(text, s, font.family_guess)[0] <= el.canonical.width)
    if size != font.size_px:
        font = font.model_copy(update={"size_px": size})
    dest = _next_asset(scene_dir, el.id, "txt")
    w, h = write_text_texture(dest, text, font.size_px, color, lines=lines, family=font.family_guess)
    el.canonical.text = text
    el.canonical.font = font
    el.canonical.texture = f"assets/{dest.name}"
    el.canonical.texture_pad = 0.0
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
            c.style = c.style.model_copy(update={"fill": None})   # a solid colour replaces the gradient; effects stay
        _write_styled(el, scene_dir, c.text.split("\n"), c.font, color, fonts, (c.width, c.height))
        return
    if el.kind == "text" and el.canonical.text:
        dest = _next_asset(scene_dir, el.id, "txt")
        font = el.canonical.font or FontGuess()
        w, h = write_text_texture(dest, el.canonical.text, edit_size(font.size_px, _line_h(c)), color, family=font.family_guess)
        el.canonical.texture = f"assets/{dest.name}"
        el.canonical.texture_pad = 0.0
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
