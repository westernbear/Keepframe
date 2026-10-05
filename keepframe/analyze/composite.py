from __future__ import annotations
from pathlib import Path
import cv2, numpy as np
from ..ir.schema import Element, Scene
from ..ir.paths import scene_asset_path
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
    prem = tex.copy()
    prem[..., :3] *= prem[..., 3:4]
    _composite_premultiplied(canvas, prem, A, opacity)


def _composite_premultiplied(canvas: np.ndarray, prem: np.ndarray, A: np.ndarray, opacity: float) -> None:
    A = np.asarray(A, dtype=np.float64)
    H, W = canvas.shape[:2]
    th, tw = prem.shape[:2]
    if A[0, 0] * A[1, 1] - A[0, 1] * A[1, 0] == 0:
        # OpenCV's singular inverse samples tex[0, 0] across the whole canvas.
        # Preserve that legacy behavior even for zero canonical width/height.
        x0, y0, x1, y1 = 0, 0, W, H
    else:
        # Include bilinear support one source pixel beyond the texture, then
        # pad two destination pixels. Source padding matters for large scales.
        corners = np.array([[-1, -1, 1], [tw, -1, 1], [tw, th, 1], [-1, th, 1]]) @ A.T
        lo = np.floor(corners.min(axis=0)).astype(int) - 2
        hi = np.ceil(corners.max(axis=0)).astype(int) + 2
        x0, y0 = max(0, lo[0]), max(0, lo[1])
        x1, y1 = min(W, hi[0]), min(H, hi[1])
        if x1 <= x0 or y1 <= y0:
            return
    roi_affine = A.copy()
    roi_affine[:, 2] -= (x0, y0)
    # ponytail: ROI-local interpolation permits 1e-3 pixel drift; use full-canvas
    # warps if stricter pixel parity becomes a requirement.
    warped = cv2.warpAffine(prem, roi_affine, (x1 - x0, y1 - y0), flags=cv2.INTER_LINEAR,
                            borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    a = warped[..., 3:4] * opacity
    roi = canvas[y0:y1, x0:x1]
    roi *= (1.0 - a)
    roi += warped[..., :3] * opacity


def composite_scene(scene: Scene, scene_dir: Path, f: int, cache: dict | None = None) -> np.ndarray:
    W, H = scene.size
    canvas = np.empty((H, W, 3), np.float32)
    cache = {} if cache is None else cache
    if scene.background.kind == "image":
        path = scene_asset_path(scene_dir, scene.background.value)
        key = ("background", scene.background.value, scene.size)
        bg = cache.get(key)
        if bg is None:
            bg = cache[key] = cv2.resize(load_texture(path)[..., :3], (W, H))
        canvas[:] = bg
    else:
        canvas[:] = hex_to_rgb(scene.background.value)
    order = sorted((e for e in scene.elements if e.visible[0] <= f <= e.visible[1]), key=lambda e: eval_z(e, f))
    for el in order:
        if not el.canonical.texture:
            continue
        tex = cache.get(el.canonical.texture)
        if tex is None:
            tex = cache[el.canonical.texture] = load_texture(Path(scene_dir) / el.canonical.texture)
        prem_key = ("premultiplied", el.canonical.texture)
        prem = cache.get(prem_key)
        if prem is None:
            prem = tex.copy()
            prem[..., :3] *= prem[..., 3:4]
            cache[prem_key] = prem
        p = eval_props(el, f)
        # ponytail: kind "3d" uses canonical.texture as a static preview here;
        # animated GLB rotation belongs to the Three.js composer. Sprites ignore rx/ry.
        reveal = float(np.clip(p["reveal"], 0.0, 1.0))
        if reveal < 1.0:
            prem = prem.copy()  # never clip either shared full-texture cache
            prem[:, int(round(prem.shape[1] * reveal)):] = 0
        _composite_premultiplied(canvas, prem, texture_to_scene_affine(el, p, tex.shape[:2]), p["opacity"])
    return canvas
