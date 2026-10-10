from __future__ import annotations

import base64
import json
import mimetypes
import os
from pathlib import Path
from typing import Callable

import cv2

from ..ir.gradient import gradient_at, render_gradient
from ..ir.schema import DEFAULTS, Element, Scene, Track, UIComponent
from ..ir.paths import scene_asset_path
from ..ir.tracks import eval_track
from ..jobs.spec import JobSpec
from .plan import (
    PlanConflict,
    load_render_plan,
    load_render_plan_scene,
    load_render_plan_state,
    validate_render_plan_execution,
)

VENDOR = Path(__file__).parents[1] / "compose" / "vendor" / "lottie-5.12.2.min.js"
AssetResolver = Callable[[str], tuple[bytes, str]]


def preflight_lottie(scene: Scene) -> None:
    bg = scene.background
    if bg.kind not in {"color", "image", "gradient", "video"}:
        raise PlanConflict(f"Lottie does not support background kind {bg.kind}")
    if bg.kind == "video" or (bg.kind == "gradient" and len(bg.gradient_keys) > 1):
        raise PlanConflict(f"Lottie does not animate a {bg.kind} background; export HTML or After Effects")
    for element in scene.elements:
        if element.canonical.video:
            raise PlanConflict(f"Lottie does not animate video sprite {element.id}; export HTML or After Effects")
        if element.kind == "3d" and not element.canonical.texture:
            raise PlanConflict(f"Lottie 3D element {element.id} requires a canonical texture fallback")
        if element.kind != "3d" and element.canonical.model and not element.canonical.texture:
            raise PlanConflict(f"Lottie does not support 3D element {element.id}")
        if any(abs(key.v) > 1e-9 for prop in ("skx", "sky") for key in (element.tracks[prop].keys if prop in element.tracks else ())):
            raise PlanConflict(f"Lottie does not support skew on element {element.id}")
        if len({key.v for key in element.z.keys}) > 1:
            raise PlanConflict(f"Lottie does not support dynamic z on element {element.id}")


def _hex(value: str | None, fallback: str = "#000000") -> list[float]:
    raw = (value or fallback).lstrip("#")
    if len(raw) == 3:
        raw = "".join(char * 2 for char in raw)
    try:
        return [int(raw[index:index + 2], 16) / 255 for index in (0, 2, 4)]
    except (ValueError, IndexError):
        return _hex(fallback, "#000000")


def _ease_at(track: Track | None, frame: int):
    if track is None:
        return None
    for left, right in zip(track.keys, track.keys[1:]):
        if left.t <= frame < right.t:
            return left.ease
    return None


def _scalar(track: Track | None, default: float, multiplier: float = 1.0) -> dict:
    if track is None or len(track.keys) == 1:
        value = track.keys[0].v if track else default
        return {"a": 0, "k": value * multiplier}
    keys = []
    for index, key in enumerate(track.keys):
        item = {"t": key.t, "s": [key.v * multiplier]}
        if index + 1 < len(track.keys):
            nxt = track.keys[index + 1]
            item["e"] = [nxt.v * multiplier]
            if key.ease:
                x1, y1, x2, y2 = key.ease
                item.update({"o": {"x": [x1], "y": [y1]}, "i": {"x": [x2], "y": [y2]}})
        keys.append(item)
    return {"a": 1, "k": keys}


def _vector(
    first: Track | None,
    second: Track | None,
    defaults: tuple[float, float],
    multiplier: float = 1.0,
) -> dict:
    times = sorted({key.t for track in (first, second) if track for key in track.keys})
    if len(times) <= 1:
        return {
            "a": 0,
            "k": [
                (first.keys[0].v if first else defaults[0]) * multiplier,
                (second.keys[0].v if second else defaults[1]) * multiplier,
                0,
            ],
        }
    keys = []
    for index, frame in enumerate(times):
        values = [
            eval_track(first, frame) if first else defaults[0],
            eval_track(second, frame) if second else defaults[1],
        ]
        item = {"t": frame, "s": [value * multiplier for value in values] + [0]}
        if index + 1 < len(times):
            next_frame = times[index + 1]
            next_values = [
                eval_track(first, next_frame) if first else defaults[0],
                eval_track(second, next_frame) if second else defaults[1],
            ]
            item["e"] = [value * multiplier for value in next_values] + [0]
            eases = [_ease_at(first, frame), _ease_at(second, frame)]
            item["o"] = {
                "x": [(ease or (0, 0, 1, 1))[0] for ease in eases],
                "y": [(ease or (0, 0, 1, 1))[1] for ease in eases],
            }
            item["i"] = {
                "x": [(ease or (0, 0, 1, 1))[2] for ease in eases],
                "y": [(ease or (0, 0, 1, 1))[3] for ease in eases],
            }
        keys.append(item)
    return {"a": 1, "k": keys}


def _transform(element: Element) -> dict:
    # Sprites ignore rx/ry; 3D elements export the canonical static texture.
    tracks = element.tracks
    anchor = element.canonical.anchor
    return {
        "o": _scalar(tracks.get("opacity"), DEFAULTS["opacity"], 100),
        "r": _scalar(tracks.get("rot"), DEFAULTS["rot"]),
        "p": _vector(tracks.get("x"), tracks.get("y"), (DEFAULTS["x"], DEFAULTS["y"])),
        "a": {"a": 0, "k": [anchor[0] * element.canonical.width, anchor[1] * element.canonical.height, 0]},
        "s": _vector(tracks.get("sx"), tracks.get("sy"), (DEFAULTS["sx"], DEFAULTS["sy"]), 100),
    }


def _reveal_mask(element: Element) -> dict | None:
    track = element.tracks.get("reveal")
    if track is None or all(key.v == 1 for key in track.keys):
        return None
    c = element.canonical

    def rectangle(value: float) -> dict:
        width = c.width * max(0.0, min(1.0, value))
        # Lottie text is positioned at its baseline, with glyphs above zero.
        top = -c.height if element.kind == "text" else 0
        return {"i": [[0, 0]] * 4, "o": [[0, 0]] * 4,
                "v": [[0, top], [width, top], [width, c.height], [0, c.height]], "c": True}

    if len(track.keys) == 1:
        path = {"a": 0, "k": rectangle(track.keys[0].v)}
    else:
        keys = []
        for index, key in enumerate(track.keys):
            item = {"t": key.t, "s": [rectangle(key.v)]}
            if index + 1 < len(track.keys):
                item["e"] = [rectangle(track.keys[index + 1].v)]
                x1, y1, x2, y2 = key.ease or (0, 0, 1, 1)
                item.update(o={"x": [x1], "y": [y1]}, i={"x": [x2], "y": [y2]})
            keys.append(item)
        path = {"a": 1, "k": keys}
    return {"inv": False, "mode": "a", "pt": path, "o": {"a": 0, "k": 100}, "x": {"a": 0, "k": 0}}


def _export_report(scene: Scene) -> dict:
    # ponytail: Lottie supports static 3D texture previews; preserve animated
    # models through the native Three.js backend until a 3D export path exists.
    return {"warnings": [f"3D element {element.id} exported as a static canonical texture; rx/ry rotation is not rendered."
                         for element in scene.elements if element.kind == "3d"]}


def _text_layer(index: int, text: str, x: float, y: float, size: float, color: str, frames: int, font: str = "sans-serif") -> dict:
    return {
        "ddd": 0, "ind": index, "ty": 5, "nm": f"text-{index}", "sr": 1,
        "ks": {
            "o": {"a": 0, "k": 100}, "r": {"a": 0, "k": 0},
            "p": {"a": 0, "k": [x, y, 0]}, "a": {"a": 0, "k": [0, 0, 0]},
            "s": {"a": 0, "k": [100, 100, 100]},
        },
        "t": {"a": [], "p": {}, "m": {"g": 1, "a": {"a": 0, "k": [0, 0]}}, "d": {"k": [{"s": {"f": font, "s": size, "t": text, "j": 0, "tr": 0, "lh": size * 1.2, "fc": _hex(color)}, "t": 0}]}},
        "ip": 0, "op": frames, "st": 0, "bm": 0,
    }


def _ui_layers(components: list[UIComponent], frames: int) -> list[dict]:
    layers: list[dict] = []
    origin_x = min((component.bbox[0] for component in components), default=0)
    origin_y = min((component.bbox[1] for component in components), default=0)

    def add(component: UIComponent) -> None:
        x0, y0, x1, y1 = component.bbox
        width, height = max(0.0, x1 - x0), max(0.0, y1 - y0)
        props = component.props
        index = len(layers) + 1
        layers.append({
            "ddd": 0, "ind": index, "ty": 4, "nm": component.id, "sr": 1,
            "ks": {
                "o": {"a": 0, "k": 100}, "r": {"a": 0, "k": 0},
                "p": {"a": 0, "k": [x0 - origin_x, y0 - origin_y, 0]}, "a": {"a": 0, "k": [0, 0, 0]},
                "s": {"a": 0, "k": [100, 100, 100]},
            },
            "shapes": [{"ty": "gr", "it": [
                {"ty": "rc", "p": {"a": 0, "k": [width / 2, height / 2]}, "s": {"a": 0, "k": [width, height]}, "r": {"a": 0, "k": float(props.get("radius", 0) or 0)}},
                {"ty": "fl", "c": {"a": 0, "k": _hex(str(props.get("background", "#1f2937"))) + [1]}, "o": {"a": 0, "k": 100}},
                {"ty": "tr", "p": {"a": 0, "k": [0, 0]}, "a": {"a": 0, "k": [0, 0]}, "s": {"a": 0, "k": [100, 100]}, "r": {"a": 0, "k": 0}, "o": {"a": 0, "k": 100}},
            ]}],
            "ip": 0, "op": frames, "st": 0, "bm": 0,
        })
        if component.text:
            layers.append(_text_layer(len(layers) + 1, component.text, x0 - origin_x + 8, y0 - origin_y + min(height, 24), float(props.get("font_size", 16) or 16), str(props.get("color", "#ffffff")), frames))
        for child in component.children:
            add(child)

    for component in components:
        add(component)
    return layers


def animation_from_scene(scene: Scene, resolve_asset: AssetResolver) -> dict:
    preflight_lottie(scene)
    assets: list[dict] = []
    layers: list[dict] = [{
        "ddd": 0, "ind": 1, "ty": 1, "nm": "background", "sw": scene.size[0], "sh": scene.size[1],
        "sc": scene.background.value, "ks": {"o": {"a": 0, "k": 100}, "r": {"a": 0, "k": 0}, "p": {"a": 0, "k": [0, 0, 0]}, "a": {"a": 0, "k": [0, 0, 0]}, "s": {"a": 0, "k": [100, 100, 100]}},
        "ip": 0, "op": scene.frames, "st": 0, "bm": 0,
    }]
    if scene.background.kind in {"image", "gradient"}:
        if scene.background.kind == "image":
            content, mime = resolve_asset(scene.background.value)
        else:
            buf = cv2.imencode(".png", cv2.cvtColor(render_gradient(gradient_at(scene.background, 0), *scene.size), cv2.COLOR_RGB2BGR))[1]
            content, mime = buf.tobytes(), "image/png"
        asset_id = "image-background"
        assets.append({"id": asset_id, "w": scene.size[0], "h": scene.size[1], "u": "",
                       "p": f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}", "e": 1})
        layers[0].update(ty=2, refId=asset_id)
        for key in ("sw", "sh", "sc"):
            del layers[0][key]
    fonts: set[str] = set()
    ordered = sorted(scene.elements, key=lambda element: element.z.keys[0].v)
    for element in ordered:
        if element.kind == "group":
            continue
        index = len(layers) + 1
        common = {
            "ddd": 0, "ind": index, "nm": element.id, "sr": 1, "ks": _transform(element),
            "ip": element.visible[0], "op": element.visible[1] + 1, "st": 0, "bm": 0,
        }
        mask = _reveal_mask(element)
        if mask is not None:
            common.update(hasMask=True, masksProperties=[mask])
        canonical = element.canonical
        if element.kind == "text" and canonical.text is not None:
            font = canonical.font.family_guess if canonical.font else "sans-serif"
            fonts.add(font)
            size = canonical.font.size_px if canonical.font else 32
            layers.append({**common, "ty": 5, "t": {"a": [], "p": {}, "m": {"g": 1, "a": {"a": 0, "k": [0, 0]}}, "d": {"k": [{"s": {"f": font, "s": size, "t": canonical.text, "j": 0, "tr": 0, "lh": canonical.height, "fc": _hex(canonical.color)}, "t": 0}]}}})
        elif element.kind == "ui":
            components = [] if scene.ui is None else [component for component in scene.ui.components if component.id == element.id]
            asset_id = f"ui-{element.id}"
            assets.append({"id": asset_id, "w": round(canonical.width), "h": round(canonical.height), "layers": _ui_layers(components, scene.frames)})
            layers.append({**common, "ty": 0, "refId": asset_id, "w": round(canonical.width), "h": round(canonical.height)})
        elif canonical.texture:
            content, mime = resolve_asset(canonical.texture)
            asset_id = f"image-{len(assets) + 1}"
            assets.append({"id": asset_id, "w": round(canonical.width), "h": round(canonical.height), "u": "", "p": f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}", "e": 1})
            layers.append({**common, "ty": 2, "refId": asset_id})
    return {
        "v": "5.12.2", "fr": scene.fps, "ip": 0, "op": scene.frames,
        "w": scene.size[0], "h": scene.size[1], "nm": scene.id, "ddd": 0,
        # ponytail: normalized ascent keeps local-font text finite in lottie-web;
        # export exact per-font metrics when the IR carries them.
        "assets": assets, "fonts": {"list": [{"fName": font, "fFamily": font, "fStyle": "Regular", "ascent": 75} for font in sorted(fonts)]},
        "meta": _export_report(scene),
        "layers": list(reversed(layers)),
    }


def write_lottie(scene: Scene, scene_dir: Path, output: Path) -> Path:
    scene_dir = Path(scene_dir)

    def resolve(raw: str) -> tuple[bytes, str]:
        path = scene_asset_path(scene_dir, raw)
        return path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(animation_from_scene(scene, resolve), ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    output.with_suffix(".report.json").write_text(json.dumps(_export_report(scene), ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def compose_lottie_player(animation: dict, output: Path) -> Path:
    payload = json.dumps(animation, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    width, height = int(animation["w"]), int(animation["h"])
    page = f'''<!doctype html><html><head><meta charset="utf-8"><style>html,body,#stage{{margin:0;width:{width}px;height:{height}px;overflow:hidden}}</style><script>{VENDOR.read_text(encoding="utf-8")}</script></head><body><div id="stage"></div><script>const animation=lottie.loadAnimation({{container:document.getElementById('stage'),renderer:'svg',loop:false,autoplay:false,animationData:{payload}}});window.__seek=f=>{{animation.goToAndStop(f,true);return f}};window.__bbox=()=>[0,0,0,0];const ready=()=>{{window.__seek(0);window.__ready=true}};animation.addEventListener('DOMLoaded',ready);if(animation.isLoaded)ready();</script></body></html>'''
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding="utf-8")
    return output


def _plan_resolver(root: Path, plan):
    scene_prefix = str(Path(plan.scene_project_path).parent).replace("\\", "/")
    assets = {asset.project_path: asset for asset in plan.assets}
    pinned = root / "renders" / plan.id / "assets"

    def resolve(raw: str) -> tuple[bytes, str]:
        project_path = raw if raw.startswith(scene_prefix + "/") else f"{scene_prefix}/{raw}"
        asset = assets.get(project_path)
        if asset is None:
            raise PlanConflict(f"Lottie asset is not pinned: {raw}")
        return (pinned / asset.id).read_bytes(), asset.media_kind

    return resolve


def prepare_lottie_job(root: Path, plan_id: str) -> JobSpec:
    root = Path(root).resolve()
    plan = load_render_plan(root, plan_id)
    state = load_render_plan_state(root, plan_id)
    if plan.backend != "lottie" or plan.mode != "final":
        raise PlanConflict("Lottie is a final-only render backend")
    if state.status != "approved" or not state.execution_id:
        raise PlanConflict("Lottie render plan must be approved")
    return JobSpec(kind="export", args={"lottie_plan_id": plan.id, "project_root": str(root), "execution_id": state.execution_id})


def export_lottie_plan(root: Path, plan_id: str, execution_id: str) -> dict[str, str]:
    root = Path(root).resolve()
    validate_render_plan_execution(root, plan_id, execution_id)
    plan = load_render_plan(root, plan_id)
    scene = load_render_plan_scene(root, plan_id)
    output = root / "renders" / plan.id / "lottie" / "animation.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps(animation_from_scene(scene, _plan_resolver(root, plan)), ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, output)
    report = output.with_suffix(".report.json")
    report.write_text(json.dumps(_export_report(scene), ensure_ascii=False, indent=2), encoding="utf-8")
    return {"animation": str(output), "report": str(report)}


__all__ = ["animation_from_scene", "compose_lottie_player", "export_lottie_plan", "preflight_lottie", "prepare_lottie_job", "write_lottie"]
