from __future__ import annotations
import base64
import html
import json
import math
import re
from pathlib import Path

from ..assets import validate_glb
from ..ir.schema import Element, Scene, UIComponent, UIModel
from ..ir.paths import scene_asset_path
from ..ir.tracks import eval_z

VENDOR = Path(__file__).parent / "vendor"
TEMPLATE = Path(__file__).parent / "template.html"


def _script_json(value: object) -> str:
    return (
        json.dumps(value, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _data_uri(path: Path, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _ui_number(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _ui_color(value: object) -> str | None:
    text = str(value)
    return text if re.fullmatch(r"#[0-9a-fA-F]{3,8}", text) else None


def _ui_component(component: UIComponent, parent_bbox: tuple[float, float, float, float] | None = None) -> str:
    tag = {"nav": "nav", "card": "section", "button": "button", "list": "ul"}.get(component.kind, "div")
    x0, y0, x1, y1 = component.bbox
    if parent_bbox is None:
        left = top = 0.0
        position = "relative"
    else:
        left, top = x0 - parent_bbox[0], y0 - parent_bbox[1]
        position = "absolute"
    props = component.props
    styles = [
        f"position:{position}", f"left:{left:.4f}px", f"top:{top:.4f}px",
        f"width:{max(0.0, x1 - x0):.4f}px", f"height:{max(0.0, y1 - y0):.4f}px",
        f"border-radius:{max(0.0, _ui_number(props.get('radius'))):.4f}px",
    ]
    if color := _ui_color(props.get("background")):
        styles.append(f"background:{color}")
    if color := _ui_color(props.get("color")):
        styles.append(f"color:{color}")
    if font_size := _ui_number(props.get("font_size")):
        styles.append(f"font-size:{max(1.0, font_size):.4f}px")
    states = html.escape(json.dumps([state.model_dump(mode="json") for state in component.states], separators=(",", ":")), quote=True)
    attrs = (
        f'class="ui-component ui-{component.kind}" data-ui-id="{html.escape(component.id)}" '
        f'data-ui-states="{states}" style="{html.escape(";".join(styles), quote=True)}"'
    )
    text = f'<span class="ui-component__text">{html.escape(component.text or "")}</span>'
    children = "".join(_ui_component(child, component.bbox) for child in component.children)
    if tag == "button":
        return f'<button type="button" {attrs}>{text}{children}</button>'
    return f'<{tag} {attrs}>{text}{children}</{tag}>'


def _ui_html(model: UIModel | None, element_id: str) -> str:
    if model is None:
        return ""
    component = next((item for item in model.components if item.id == element_id), None)
    return _ui_component(component) if component is not None else ""


def _element_html(el: Element, scene_dir: Path, fps: float, ui: UIModel | None) -> str:
    c = el.canonical
    ax, ay = c.anchor
    style = (
        f"left:{-ax * c.width:.4f}px;top:{-ay * c.height:.4f}px;width:{c.width:.4f}px;height:{c.height:.4f}px;"
        f"transform-origin:{ax * 100:.4f}% {ay * 100:.4f}%;"
    )
    start = el.visible[0] / fps
    dur = (el.visible[1] - el.visible[0] + 1) / fps
    if el.kind == "3d" and c.model:
        model_path = scene_asset_path(scene_dir, c.model)
        validate_glb(model_path.read_bytes())
        inner = f'<canvas class="model-canvas" width="{max(1, round(c.width))}" height="{max(1, round(c.height))}" data-model="{_data_uri(model_path, "model/gltf-binary")}"></canvas>'
    elif el.kind == "ui":
        inner = _ui_html(ui, el.id)
    elif el.kind == "text" and c.text is not None and c.font is not None:
        f = c.font
        inner = (
            f'<span style="font-family:{html.escape(f.family_guess)};font-weight:{f.weight};'
            f'font-size:{f.size_px}px;line-height:{c.height}px;color:{c.color or "#000"}">{html.escape(c.text)}</span>'
        )
    elif c.texture:
        inner = f'<img src="{_data_uri(scene_dir / c.texture)}" alt="{html.escape(el.id)}">'
    else:
        inner = ""
    return (
        f'<div class="el" id="el-{html.escape(el.id)}" data-start="{start:.4f}" data-duration="{dur:.4f}" '
        f'data-track-index="{eval_z(el, 0)}" style="{style}">{inner}</div>'
    )


def compose(scene: Scene, scene_dir: Path, out_html: Path) -> Path:
    scene_dir, out_html = Path(scene_dir), Path(out_html)
    elements = "\n".join(_element_html(e, scene_dir, scene.fps, scene.ui) for e in scene.elements)
    scene_json = _script_json(scene.model_dump(by_alias=True))
    page = TEMPLATE.read_text()
    bg = scene.background.value if scene.background.kind == "color" else (
        f'#000 url("{_data_uri(scene_asset_path(scene_dir, scene.background.value))}") 0 0/100% 100% no-repeat')
    for k, v in {
        "{{ID}}": html.escape(scene.id),
        "{{WIDTH}}": str(scene.size[0]),
        "{{HEIGHT}}": str(scene.size[1]),
        "{{BG}}": bg,
        "{{GSAP_JS}}": (VENDOR / "gsap.min.js").read_text(),
        "{{CUSTOMEASE_JS}}": (VENDOR / "CustomEase.min.js").read_text(),
        "{{THREE_JS}}": (VENDOR / "three-0.128.0.min.js").read_text(),
        "{{GLTFLOADER_JS}}": (VENDOR / "GLTFLoader-0.128.0.js").read_text(),
        "{{ELEMENTS}}": elements,
        "{{SCENE_JSON}}": scene_json,
    }.items():
        page = page.replace(k, v)
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(page)
    return out_html
