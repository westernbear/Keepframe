from __future__ import annotations
import base64
import html
import json
from pathlib import Path

from ..ir.schema import Element, Scene
from ..ir.tracks import eval_z

VENDOR = Path(__file__).parent / "vendor"
TEMPLATE = Path(__file__).parent / "template.html"


def _data_uri(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _element_html(el: Element, scene_dir: Path, fps: float) -> str:
    c = el.canonical
    ax, ay = c.anchor
    style = (
        f"left:{-ax * c.width:.4f}px;top:{-ay * c.height:.4f}px;width:{c.width:.4f}px;height:{c.height:.4f}px;"
        f"transform-origin:{ax * 100:.4f}% {ay * 100:.4f}%;"
    )
    start = el.visible[0] / fps
    dur = (el.visible[1] - el.visible[0] + 1) / fps
    if el.kind == "text" and c.text is not None and c.font is not None:
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
    elements = "\n".join(_element_html(e, scene_dir, scene.fps) for e in scene.elements)
    scene_json = json.dumps(scene.model_dump(by_alias=True), separators=(",", ":"))
    page = TEMPLATE.read_text()
    for k, v in {
        "{{ID}}": html.escape(scene.id),
        "{{WIDTH}}": str(scene.size[0]),
        "{{HEIGHT}}": str(scene.size[1]),
        "{{BG}}": scene.background.value if scene.background.kind == "color" else "#000000",
        "{{GSAP_JS}}": (VENDOR / "gsap.min.js").read_text(),
        "{{CUSTOMEASE_JS}}": (VENDOR / "CustomEase.min.js").read_text(),
        "{{ELEMENTS}}": elements,
        "{{SCENE_JSON}}": scene_json,
    }.items():
        page = page.replace(k, v)
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(page)
    return out_html
