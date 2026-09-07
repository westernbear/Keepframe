from __future__ import annotations
from pathlib import Path
import cv2, numpy as np
from ..ir.schema import Element, Scene
from ..ir.tracks import affine_matrix, eval_props, eval_z


def hex_to_rgb(s: str) -> tuple[float, float, float]:
    s = s.lstrip("#")
    return tuple(int(s[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


def load_texture(path: Path) -> np.ndarray:
    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise FileNotFoundError(path)
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA)
    elif img.shape[2] == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
    rgba = cv2.cvtColor(img, cv2.COLOR_BGRA2RGBA).astype(np.float32) / 255.0
    return rgba


def texture_to_scene_affine(el: Element, props: dict[str, float], tex_shape: tuple[int, int]) -> np.ndarray:
    th, tw = tex_shape
    c = el.canonical
    ax, ay = c.anchor
    # texture pixel -> local canonical coords (anchor at origin)
    L = np.array([[c.width / tw, 0.0, -ax * c.width], [0.0, c.height / th, -ay * c.height], [0.0, 0.0, 1.0]])
    M = affine_matrix(props) @ L
    return M[:2, :]


def composite_element(canvas: np.ndarray, tex: np.ndarray, A: np.ndarray, opacity: float) -> None:
    H, W = canvas.shape[:2]
    prem = tex.copy()
    prem[..., :3] *= prem[..., 3:4]
    warped = cv2.warpAffine(prem, A, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    a = warped[..., 3:4] * opacity
    canvas *= (1.0 - a)
    canvas += warped[..., :3] * opacity


def composite_scene(scene: Scene, scene_dir: Path, f: int, cache: dict | None = None) -> np.ndarray:
    W, H = scene.size
    canvas = np.empty((H, W, 3), np.float32)
    canvas[:] = hex_to_rgb(scene.background.value) if scene.background.kind == "color" else (0, 0, 0)
    cache = {} if cache is None else cache
    order = sorted((e for e in scene.elements if e.visible[0] <= f <= e.visible[1]), key=lambda e: eval_z(e, f))
    for el in order:
        if not el.canonical.texture:
            continue
        tex = cache.get(el.canonical.texture)
        if tex is None:
            tex = cache[el.canonical.texture] = load_texture(Path(scene_dir) / el.canonical.texture)
        p = eval_props(el, f)
        composite_element(canvas, tex, texture_to_scene_affine(el, p, tex.shape[:2]), p["opacity"])
    return canvas
