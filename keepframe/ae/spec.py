"""Describe a scene as deterministic data for the After Effects extension."""

import hashlib
import json
import math
import re
import struct
import zlib
from pathlib import Path

from PIL import Image

from ..ir.paths import scene_asset_path
from ..ir.schema import DEFAULTS, Element, FontGuess, Keyframe, Scene, Track
from ..ir.tracks import eval_track, eval_z


def _rounded(value):
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, dict):
        return {key: _rounded(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_rounded(item) for item in value]
    return value


def ease_to_ae(ease, v0, v1, dt) -> dict:
    """Convert one scalar segment's bezier slopes into AE temporal easing."""
    if dt <= 0:
        raise ValueError("ease segment duration must be positive")
    x1, y1, x2, y2 = ease
    avg = (v1 - v0) / dt
    return _rounded({
        "out": [min(100, max(0.1, x1 * 100)), avg * (y1 / x1 if x1 > 0 else 0)],
        "in": [min(100, max(0.1, (1 - x2) * 100)), avg * ((1 - y2) / (1 - x2) if x2 < 1 else 0)],
    })


def png_size(path: Path) -> tuple[int, int]:
    """Read dimensions from the PNG's first (IHDR) chunk without decoding pixels."""
    with path.open("rb") as file:
        header = file.read(33)
    if (len(header) != 33 or header[:8] != b"\x89PNG\r\n\x1a\n"
            or header[8:16] != b"\0\0\0\rIHDR"):
        raise ValueError(f"{path.name} is not a PNG with a valid IHDR")
    width, height = struct.unpack(">II", header[16:24])
    if not width or not height:
        raise ValueError(f"{path.name} has invalid PNG dimensions")
    return width, height


def png_mean_color(path: Path) -> str:
    """Mean of opaque pixels in 8-bit RGB/RGBA PNGs; unsupported forms are white."""
    try:
        width, height = png_size(path)
        data = path.read_bytes()
        depth, color, compression, filtering, interlace = data[24:29]
        if depth != 8 or color not in (2, 6) or compression or filtering or interlace:
            return "#ffffff"
        compressed, offset, ended, transparent = bytearray(), 8, False, None
        while offset + 12 <= len(data):
            length = struct.unpack_from(">I", data, offset)[0]
            end = offset + 12 + length
            if end > len(data):
                return "#ffffff"
            kind, payload = data[offset + 4:offset + 8], data[offset + 8:end - 4]
            if zlib.crc32(kind + payload) != struct.unpack_from(">I", data, end - 4)[0]:
                return "#ffffff"
            if kind == b"IDAT":
                compressed.extend(payload)
            elif kind == b"tRNS":
                if color != 2 or length != 6:
                    return "#ffffff"
                transparent = struct.unpack(">HHH", payload)
            elif kind == b"IEND":
                ended = length == 0
                break
            offset = end
        channels = 3 if color == 2 else 4
        stride = width * channels
        expected = (stride + 1) * height
        decoder = zlib.decompressobj()
        pixels = decoder.decompress(compressed, expected + 1)
        if not ended or len(pixels) != expected or not decoder.eof or decoder.unused_data:
            return "#ffffff"
        previous, totals, count = bytes(stride), [0, 0, 0], 0
        for offset in range(0, expected, stride + 1):
            filter_type = pixels[offset]
            if filter_type > 4:
                return "#ffffff"
            row = bytearray(pixels[offset + 1:offset + 1 + stride])
            for i, value in enumerate(row):
                left = row[i - channels] if i >= channels else 0
                up = previous[i]
                corner = previous[i - channels] if i >= channels else 0
                predictor = 0
                if filter_type == 1:
                    predictor = left
                elif filter_type == 2:
                    predictor = up
                elif filter_type == 3:
                    predictor = (left + up) // 2
                elif filter_type == 4:
                    p = left + up - corner
                    predictor = min((left, up, corner), key=lambda v: abs(p - v))
                row[i] = (value + predictor) & 255
            for i in range(0, stride, channels):
                rgb = tuple(row[i:i + 3])
                if (channels == 4 and row[i + 3] < 128) or rgb == transparent:
                    continue
                count += 1
                for j in range(3):
                    totals[j] += rgb[j]
            previous = row
        return "#" + "".join(f"{round(total / count):02x}" for total in totals) if count else "#ffffff"
    except (OSError, ValueError, struct.error, zlib.error):
        return "#ffffff"


def _color(value: str) -> str:
    if not re.fullmatch(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?", value):
        raise ValueError(f"invalid RGB color: {value}")
    digits = value[1:].lower()
    return "#" + ("".join(char * 2 for char in digits) if len(digits) == 3 else digits)


def _keys(samples, fps):
    keys = [[frame, value, None, None] for frame, value, _ in samples]
    for i, ((t0, v0, ease), (t1, v1, _)) in enumerate(zip(samples, samples[1:])):
        if ease is None:
            continue
        a = v0 if isinstance(v0, list) else [v0]
        b = v1 if isinstance(v1, list) else [v1]
        converted = [ease_to_ae(ease, start, end, (t1 - t0) / fps) for start, end in zip(a, b)]
        keys[i][2] = [item["out"] for item in converted]
        keys[i + 1][3] = [item["in"] for item in converted]
    return keys


def _scalar(track, fps, factor=1, offset=0):
    return _keys([(key.t, offset + key.v * factor, key.ease) for key in track.keys], fps)


def _tracks(tracks):
    return {prop: tracks[prop] if prop in tracks else Track(keys=[Keyframe(t=0, v=value)])
            for prop, value in DEFAULTS.items()}


def _scale(tracks, fix, fps, warnings, eid):
    sx, sy = tracks["sx"], tracks["sy"]
    xkeys, ykeys = {key.t: key for key in sx.keys}, {key.t: key for key in sy.keys}
    if xkeys.keys() != ykeys.keys():
        warnings.append(f"scale x/y keyframes of {eid} merged")
    samples = []
    for frame in sorted(xkeys.keys() | ykeys.keys()):
        ease = (xkeys[frame] if frame in xkeys else ykeys[frame]).ease
        samples.append((frame, [eval_track(sx, frame) * 100 * fix[0],
                                eval_track(sy, frame) * 100 * fix[1]], ease))
    return _keys(samples, fps)


def _props(tracks, fix, fps, warnings, eid):
    return {"position_x": _scalar(tracks["x"], fps), "position_y": _scalar(tracks["y"], fps),
            "scale": _scale(tracks, fix, fps, warnings, eid), "rotation": _scalar(tracks["rot"], fps),
            "opacity": _scalar(tracks["opacity"], fps, 100)}


def _effects(tracks, fps, warnings, eid):
    effects = {"skew": None, "reveal": None}
    if any(key.v != 0 for key in tracks["sky"].keys):
        warnings.append(f"y skew of {eid} is not represented")
    if any(key.v != 0 for key in tracks["skx"].keys):
        frames = sorted({key.t for prop in ("skx", "sx", "sy") for key in tracks[prop].keys})
        samples = []
        for frame in frames:
            skew, sx, sy = (eval_track(tracks[prop], frame) for prop in ("skx", "sx", "sy"))
            shear = math.tan(math.radians(skew)) * sy
            if sx == 0 and shear != 0:
                raise ValueError(f"x scale of {eid} is zero; skew cannot be represented")
            angle = math.degrees(math.atan(shear / sx)) if sx else 0
            samples.append((frame, angle, None))
        effects["skew"] = {"skew": _keys(samples, fps), "axis": 0}
    if any(key.v != 1 for key in tracks["reveal"].keys):
        effects["reveal"] = {"completion": _scalar(tracks["reveal"], fps, -100, 100),
                             "angle": 270, "feather": 0}
    return effects


def _font(guess, fonts, warnings, text):
    if fonts is None:
        return {"postscript": None, "family": guess.family_guess, "style": None, "substituted": False}

    def rank(font):
        style = re.sub(r"[^a-z]", "", (font["style"] or "Regular").casefold())
        weights = (("thin", 100), ("extralight", 200), ("ultralight", 200),
                   ("light", 300), ("medium", 500), ("semibold", 600), ("demibold", 600),
                   ("extrabold", 800), ("ultrabold", 800), ("bold", 700), ("black", 900), ("heavy", 900))
        weight_style = style.replace("italic", "").replace("oblique", "")
        weight = next((weight for name, weight in weights if name == weight_style), 400)
        return abs(weight - guess.weight), "italic" in style or "oblique" in style

    malgun = [font for font in fonts if font["family"].casefold() == "malgun gothic"] if re.search(
        r"[\uac00-\ud7a3\u1100-\u11ff\u3130-\u318f]", text) else []
    for family in [guess.family_guess, *guess.candidates]:
        matches = [font for font in fonts if font["family"].casefold() == family.casefold()]
        if matches:
            font = min(matches, key=rank)
            return {"postscript": font["postscript"], "family": font["family"],
                    "style": font["style"], "substituted": False}
        if malgun:
            break  # A missing detected family needs Hangul coverage before other candidates.
    bold = guess.weight >= 600
    postscript, style = ("Arial-BoldMT", "Bold") if bold else ("ArialMT", "Regular")
    family = "Arial"
    if malgun:
        family, postscript = "Malgun Gothic", "MalgunGothicBold" if bold else "MalgunGothic"
        matching_style = next((font for font in malgun if (font["style"] or "Regular").casefold() == style.casefold()), None)
        if matching_style and matching_style["postscript"]:
            postscript = matching_style["postscript"]
    warnings.append(f"font {guess.family_guess} not installed; using {family} {style}")
    return {"postscript": postscript, "family": family, "style": style, "substituted": True}


def _asset(path, name, assets):
    data = path.read_bytes()
    entry = {"name": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    if name in assets and assets[name] != entry:
        raise ValueError(f"conflicting assets named {name}")
    assets[name] = entry
    return name


def _asset_source(value, sid, scene_dir, extension=None):
    path = scene_asset_path(scene_dir, value)
    sid = re.sub(r"[^A-Za-z0-9_-]", "_", sid)
    extension = extension or image_format(path)
    return path, f"{sid}.{extension}"


def image_format(path):
    with path.open("rb") as stream:
        header = stream.read(12)
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "webp"
    raise ValueError(f"{path.name} is not a supported image (PNG, JPEG or WebP)")


def _bg_image(background) -> str | None:
    if background.kind == "image":
        return background.value
    return background.poster if background.kind in {"video", "gradient"} else None


def spec_asset_paths(scene: Scene, scene_dir: Path) -> dict[str, Path]:
    """Map only exported asset names to their scene-contained source files."""
    sources = []
    if _bg_image(scene.background):
        sources.append((_bg_image(scene.background), "background", None))
    for el in scene.elements:
        if el.kind == "text" and el.canonical.font is not None:
            continue
        if el.kind == "3d" and el.canonical.model:
            sources.append((el.canonical.model, el.id, "glb"))
        elif el.canonical.texture:
            sources.append((el.canonical.texture, el.id, None))
    assets, paths = {}, {}
    for value, sid, extension in sources:
        path, name = _asset_source(value, sid, scene_dir, extension)
        _asset(path, name, assets)  # Apply the spec's collision check too.
        paths[name] = path
    return paths


def _image(value, sid, size, anchor, scene_dir, assets):
    path, name = _asset_source(value, sid, scene_dir)
    if name.endswith(".png"):
        width, height = png_size(path)
    else:
        try:
            with Image.open(path) as image:
                width, height = image.size
        except (OSError, ValueError) as exc:
            raise ValueError(f"{path.name} is not a valid image: {exc}") from None
    name = _asset(path, name, assets)
    return ({"asset": name, "scale_fix": [size[0] / width, size[1] / height]},
            [anchor[0] * width, anchor[1] * height])


def _layer(el: Element, scene_dir, assets, fps, fonts, label):
    canonical = el.canonical
    sid = el.id
    warnings = []
    kind, source, fix = "null", None, [1, 1]
    anchor = [canonical.anchor[0] * canonical.width, canonical.anchor[1] * canonical.height]
    if el.kind == "text" and canonical.font is not None:
        kind, anchor = "text", None
        guess = canonical.font
        source = {"text": canonical.text or "", "font": _font(guess, fonts, warnings, canonical.text or ""),
                  "size_px": guess.size_px, "color": _color(canonical.color or "#000"),
                  "anchor_fraction": list(canonical.anchor), "box": [canonical.width, canonical.height]}
    elif el.kind == "3d" and canonical.model:
        kind = "model"
        source = {"asset": _asset(*_asset_source(canonical.model, sid, scene_dir, "glb"), assets),
                  "fit_box": [canonical.width, canonical.height]}
    else:
        if el.kind == "3d":
            warnings.append("3D candidate")
        if canonical.texture:
            kind = "image"
            source, anchor = _image(canonical.texture, sid, (canonical.width, canonical.height),
                                    canonical.anchor, scene_dir, assets)
            fix = source["scale_fix"]
            if el.kind == "text":
                source["text"] = canonical.text or ""
                warnings.append(f"text {sid} kept as an image (no font detected)")
        else:
            warnings.append(f"{el.id} has no image; not drawn in AE")
    tracks = _tracks(el.tracks)
    props = _props(tracks, fix, fps, warnings, el.id)
    if kind == "model":
        props.update(rotation_x=_scalar(tracks["rx"], fps), rotation_y=_scalar(tracks["ry"], fps))
    return {"id": f"kf:{el.id}", "kind": kind, "name": f"{el.id} · {el.label or el.kind}",
            "label": label, "in": el.visible[0], "out": el.visible[1],
            "source": source, "anchor": anchor, "props": props,
            "effects": _effects(tracks, fps, warnings, el.id), "warnings": warnings}


def _background(scene, scene_dir, assets):
    background = scene.background
    kind, fix, anchor, warnings = "solid", [1, 1], [0, 0], []
    image = _bg_image(background)
    if image:
        kind = "image"
        source, anchor = _image(image, "background", scene.size, (0, 0), scene_dir, assets)
        fix = source["scale_fix"]
        if background.kind != "image":
            warnings.append(f"{background.kind} background exported as its poster image")
    else:
        source = {"color": _color(background.value if background.kind in {"color", "gradient"} else "#000000")}
        if background.kind != "color":
            warnings.append(f"{background.kind} background exported as a flat colour")
    return {"id": "kf:background", "kind": kind, "name": f"background · {background.kind}", "label": None,
            "in": 0, "out": scene.frames - 1, "order": 0, "source": source, "anchor": anchor,
            "props": _props(_tracks({}), fix, scene.fps, [], "background"),
            "effects": {"skew": None, "reveal": None}, "warnings": warnings}


def comp_spec(scene: Scene, scene_dir: Path, *, project: str, scene_id: str, version: str,
              fonts: list[dict] | None = None) -> dict:
    assets, labels, warnings = {}, {}, []
    for i, group in enumerate(scene.groups):
        for member in group.members:
            labels.setdefault(member, (i % 16) + 1)
    layers = [_background(scene, scene_dir, assets)]
    warnings.extend(layers[0]["warnings"])
    ordered = sorted(scene.elements, key=lambda el: eval_z(el, el.visible[0]))
    for el in ordered:
        layer = _layer(el, scene_dir, assets, scene.fps, fonts, labels.get(el.id))
        layers.append(layer)
        if el.kind == "text" and el.canonical.font is None and layer["kind"] == "image":
            canonical = el.canonical.model_copy(update={
                "font": FontGuess(family_guess="Malgun Gothic" if re.search(
                    r"[\uac00-\ud7a3\u1100-\u11ff\u3130-\u318f]", el.canonical.text or "") else "Arial",
                    size_px=el.canonical.height * 0.8),
                "color": png_mean_color(scene_asset_path(scene_dir, el.canonical.texture)),
            })
            companion = _layer(el.model_copy(update={"canonical": canonical}), scene_dir, assets,
                               scene.fps, fonts, labels.get(el.id))
            companion.update(id=f"kf:{el.id}~text", name=f"{layer['name']} · editable text", hidden=True)
            layers.append(companion)
    for order, layer in enumerate(layers):
        layer["order"] = order
    for el in scene.elements:
        first_z = eval_z(el, el.visible[0])
        if any(el.visible[0] < key.t <= el.visible[1] and int(key.v) != first_z for key in el.z.keys):
            warnings.append(f"z-order of {el.id} changes over time; ordered by its first visible frame")
    return _rounded({
        "schema": "keepframe.ae-comp/1", "project": project, "scene": scene_id, "version": version,
        "comp": {"tag": f"keepframe:{project}/{scene_id}", "name": f"Keepframe · {project} · {scene_id}",
                 "width": scene.size[0], "height": scene.size[1], "fps": scene.fps, "frames": scene.frames},
        "assets": [assets[name] for name in sorted(assets)], "layers": layers, "warnings": warnings,
    })


def comp_spec_json(scene: Scene, scene_dir: Path, *, project: str, scene_id: str, version: str,
                   fonts: list[dict] | None = None) -> str:
    spec = comp_spec(scene, scene_dir, project=project, scene_id=scene_id, version=version, fonts=fonts)
    return json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
