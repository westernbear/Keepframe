from __future__ import annotations
import math
from pathlib import Path
import cv2, numpy as np
from ..ir.schema import Element, Scene
from ..ir.paths import scene_asset_path
from ..ir.gradient import gradient_at, render_gradient
from ..ir.tracks import affine_matrix, eval_props, eval_z, glyph_alphas, glyph_edges, is_tilted, projective_matrix
from ..log import get

log = get("keepframe.analyze")


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


def texture_to_scene(el: Element, props: dict[str, float], tex_shape: tuple[int, int], size: tuple[int, int]) -> np.ndarray:
    """Texture pixel → scene: today's 2×3 affine, or the 3×3 homography (`projective_matrix`) of a tilted element."""
    th, tw = tex_shape
    c = el.canonical
    ax, ay = c.anchor
    p = _pad(c, tex_shape)
    # texture pixel -> local canonical coords (anchor at origin); a padded texture's box starts at (p, p) (R41)
    sx, sy = c.width / (tw - 2 * p), c.height / (th - 2 * p)
    L = np.array([[sx, 0.0, -p * sx - ax * c.width], [0.0, sy, -p * sy - ay * c.height], [0.0, 0.0, 1.0]])
    if is_tilted(props, el.pending_asset or el.kind):
        return projective_matrix(props, size) @ L
    M = affine_matrix(props) @ L
    return M[:2, :]


def _pad(c, tex_shape: tuple[int, int]) -> float:
    """The texture's padding past the box (styled text effects, R41); 0 when it would leave no box."""
    p = float(c.texture_pad or 0.0)
    return p if p > 0 and min(tex_shape) - 2 * p >= 1 else 0.0


def composite_element(canvas: np.ndarray, tex: np.ndarray, A: np.ndarray, opacity: float) -> None:
    prem = tex.copy()
    prem[..., :3] *= prem[..., 3:4]
    _composite_premultiplied(canvas, prem, A, opacity)


def _composite_premultiplied(canvas: np.ndarray, prem: np.ndarray, A: np.ndarray, opacity: float) -> None:
    A = np.asarray(A, dtype=np.float64)
    H, W = canvas.shape[:2]
    th, tw = prem.shape[:2]
    if len(A) == 2 and A[0, 0] * A[1, 1] - A[0, 1] * A[1, 0] == 0:
        # OpenCV's singular inverse samples tex[0, 0] across the whole canvas.
        # Preserve that legacy behavior even for zero canonical width/height.
        x0, y0, x1, y1 = 0, 0, W, H
    else:
        # Include bilinear support one source pixel beyond the texture, then
        # pad two destination pixels. Source padding matters for large scales.
        corners = np.array([[-1, -1, 1], [tw, -1, 1], [tw, th, 1], [-1, th, 1]]) @ A.T
        if len(A) == 3:   # a tilted element's homography; CSS clips a plane reaching behind the viewer (w ≤ 0)
            if (corners[:, 2] <= 0).any() or np.linalg.det(A) == 0:   # and draws no singular one: numpy skips both
                return
            corners = corners[:, :2] / corners[:, 2:]
        lo = np.floor(corners.min(axis=0)).astype(int) - 2
        hi = np.ceil(corners.max(axis=0)).astype(int) + 2
        x0, y0 = max(0, lo[0]), max(0, lo[1])
        x1, y1 = min(W, hi[0]), min(H, hi[1])
        if x1 <= x0 or y1 <= y0:
            return
    if len(A) == 3:
        warped = cv2.warpPerspective(prem, np.array([[1, 0, -x0], [0, 1, -y0], [0, 0, 1]]) @ A, (x1 - x0, y1 - y0),
                                     flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    else:
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


def video_source_frame(offset: int, rate: float = 1.0) -> int:
    """The source frame a video layer shows `offset` frames after its start when it plays at `rate`: floor(offset ·
    rate), as AE's time stretch shows it (no half-frame offset; 1e-6 absorbs float error). The template's seek picks
    the same frame."""
    return int(math.floor(max(offset, 0) * rate + 1e-6))


def _video_frame(cache: dict, scene_dir: Path, rel: str, size: tuple[int, int], alpha: bool, i: int) -> np.ndarray | None:
    """Frame i of a video asset as float32 0..1 (RGB, or RGBA with `alpha`), None when it cannot be decoded (the
    caller draws the poster). One reader per asset lives in `cache`; a failed one is remembered."""
    from .videoasset import VideoReader
    key = ("video", rel, size, alpha)
    reader = cache.get(key)
    if reader is False:
        return None
    try:
        if reader is None:
            reader = cache[key] = VideoReader(scene_asset_path(scene_dir, rel), size, alpha=alpha)
        return reader.frame(i).astype(np.float32) / 255.0
    except Exception as e:   # fail soft: the poster
        log.warning("video %s not decoded (%s); poster drawn", Path(rel).name, type(e).__name__)
        if reader is not None:
            reader.close()
        cache[key] = False
        return None


def composite_scene(scene: Scene, scene_dir: Path, f: int, cache: dict | None = None) -> np.ndarray:
    W, H = scene.size
    canvas = np.empty((H, W, 3), np.float32)
    cache = {} if cache is None else cache
    bgd = scene.background
    image = bgd.value if bgd.kind == "image" else bgd.poster if bgd.kind == "video" else None
    video = _video_frame(cache, Path(scene_dir), bgd.value, (W, H), False, video_source_frame(f, bgd.video_rate)) \
        if bgd.kind == "video" else None
    if video is not None:
        canvas[:] = video
    elif image:
        path = scene_asset_path(scene_dir, image)
        key = ("background", image, scene.size)
        bg = cache.get(key)
        if bg is None:
            bg = cache[key] = cv2.resize(load_texture(path)[..., :3], (W, H))
        canvas[:] = bg
    elif bgd.kind == "gradient":
        g = gradient_at(bgd, f)
        key = ("background", "gradient", scene.size)
        hit = cache.get(key)
        if hit is None or hit[0] != g:
            hit = cache[key] = (g, render_gradient(g, W, H).astype(np.float32) / 255.0)
        canvas[:] = hit[1]
    elif bgd.kind == "video":
        canvas[:] = 0.0
    else:
        canvas[:] = hex_to_rgb(bgd.value)
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
        if el.canonical.video:   # a video sprite: frame f − start of its video, in the texture's geometry
            th, tw = tex.shape[:2]
            frame = _video_frame(cache, Path(scene_dir), el.canonical.video, (tw, th), True,
                                 video_source_frame(f - el.visible[0], el.canonical.video_rate))
            if frame is not None:
                prem = frame
                prem[..., :3] *= prem[..., 3:4]
        p = eval_props(el, f)
        # ponytail: kind "3d" uses canonical.texture as a static preview here;
        # animated GLB rotation belongs to the Three.js composer. 2D elements tilt by rx/ry.
        reveal = float(np.clip(p["reveal"], 0.0, 1.0))
        g = el.canonical.glyphs   # a per-glyph reveal: column i at α_i (the composer's mask-image hard stops)
        alphas = glyph_alphas(g, f - el.visible[0]) if g is not None else None
        glyphs = alphas is not None and alphas.min() < 1.0
        if reveal < 1.0 or glyphs:
            prem = prem.copy()  # never clip either shared full-texture cache
            pad = int(round(_pad(el.canonical, prem.shape[:2])))
            if pad:   # the composer's reveal and mask clip to the box (clip-path inset), effects past it included
                prem[:pad], prem[prem.shape[0] - pad:], prem[:, :pad], prem[:, prem.shape[1] - pad:] = 0, 0, 0, 0
            if reveal < 1.0:
                prem[:, pad + int(round((prem.shape[1] - 2 * pad) * reveal)):] = 0
            if glyphs:   # in texture space, before the warp: the columns follow a tilt as the CSS mask does
                w = prem.shape[1] - 2 * pad
                prem[:, pad:pad + w] *= np.repeat(alphas, np.diff(glyph_edges(g, w)))[:, None].astype(np.float32)
        _composite_premultiplied(canvas, prem, texture_to_scene(el, p, tex.shape[:2], scene.size), p["opacity"])
    return canvas
