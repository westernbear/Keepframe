from __future__ import annotations
import math
import numpy as np
from .schema import Canonical, Element, Ease, GlyphReveal, Track, PROPS, DEFAULTS

PRESET_EASES: dict[str, Ease] = {
    "linear": (0.0, 0.0, 1.0, 1.0),
    "in_quad": (0.11, 0.0, 0.5, 0.0),
    "out_quad": (0.5, 1.0, 0.89, 1.0),
    "in_out_quad": (0.45, 0.0, 0.55, 1.0),
    "in_cubic": (0.32, 0.0, 0.67, 0.0),
    "out_cubic": (0.33, 1.0, 0.68, 1.0),
    "in_out_cubic": (0.65, 0.0, 0.35, 1.0),
    "out_back": (0.34, 1.56, 0.64, 1.0),
}


def _bez(s: float, a: float, b: float) -> float:
    return 3 * (1 - s) ** 2 * s * a + 3 * (1 - s) * s ** 2 * b + s ** 3


def bezier_y(x: float, ease: Ease) -> float:
    x1, y1, x2, y2 = ease
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lo, hi, s = 0.0, 1.0, x
    for _ in range(40):  # bisection on the monotone x(s)
        if _bez(s, x1, x2) < x:
            lo = s
        else:
            hi = s
        s = 0.5 * (lo + hi)
    return _bez(s, y1, y2)


def eval_track(track: Track, f: float) -> float:
    keys = track.keys
    if f <= keys[0].t:
        return keys[0].v
    if f >= keys[-1].t:
        return keys[-1].v
    for a, b in zip(keys, keys[1:]):
        if a.t <= f <= b.t:
            u = (f - a.t) / (b.t - a.t)
            e = bezier_y(u, a.ease) if a.ease else u
            return a.v + (b.v - a.v) * e
    return keys[-1].v


def eval_props(el: Element, f: float) -> dict[str, float]:
    return {p: (eval_track(el.tracks[p], f) if p in el.tracks else DEFAULTS[p]) for p in PROPS}


def eval_z(el: Element, f: float) -> int:
    v = el.z.keys[0].v
    for k in el.z.keys:
        if k.t <= f:
            v = k.v
    return int(v)


def glyph_alphas(g: GlyphReveal, f_rel: float) -> np.ndarray:
    """Each glyph's opacity `f_rel` frames after visible[0]."""
    return np.clip((f_rel - np.asarray(g.delays)) / g.ramp, 0.0, 1.0)


def glyph_edges(g: GlyphReveal, width: float) -> list[int]:
    """Integer column edges of the glyphs across `width` px: 0, the rounded midpoints between centres, ceil(width).
    The composer's mask stops (canonical px) and numpy's texture columns (texture px) both cut here."""
    x = np.asarray(g.x)
    return [0, *np.round((x[1:] + x[:-1]) / 2 * width).astype(int).tolist(), math.ceil(width)]


def affine_matrix(p: dict[str, float]) -> np.ndarray:
    # reveal clips pixels without changing geometry; rx/ry (degrees) spin a kind
    # "3d" model and tilt a 2D element through `projective_matrix`.
    if abs(p.get("sky", 0.0)) > 1e-9:
        raise ValueError("sky must be 0 (decomposition uses skewX only)")
    r = math.radians(p["rot"])
    k = math.tan(math.radians(p["skx"]))
    R = np.array([[math.cos(r), -math.sin(r)], [math.sin(r), math.cos(r)]])
    K = np.array([[1.0, k], [0.0, 1.0]])
    S = np.diag([p["sx"], p["sy"]])
    M = np.eye(3)
    M[:2, :2] = R @ K @ S
    M[:2, 2] = [p["x"], p["y"]]
    return M


PERSPECTIVE_PER_WIDTH = 50 / 36   # ponytail: AE's default comp camera (50 mm, 36 mm film, horizontal); live AE check verifies


def perspective_px(size: tuple[int, int]) -> float:
    """The stage's CSS perspective d for a scene `size` (W, H)."""
    return size[0] * PERSPECTIVE_PER_WIDTH


def is_tilted(p: dict[str, float], kind: str) -> bool:
    """rx/ry tilt a 2D element; kind "3d" spins its model with them instead. Callers pass `el.pending_asset or
    el.kind`: a 3D candidate's rx/ry is its object's spin (solids.estimate_spin), drawn flat until its model exists."""
    return kind != "3d" and max(abs(p.get("rx", 0.0)), abs(p.get("ry", 0.0))) > 1e-6


def projective_matrix(p: dict[str, float], size: tuple[int, int]) -> np.ndarray:
    """3×3 local (anchor at origin, z = 0) → scene homography of the composer's `translate(x,y) rotateX(rx)
    rotateY(ry) rotate(rot) skewX(skx) scale(sx,sy)` under `#stage{perspective: d}` at the stage centre c:
    T(c)·Persp(d)·T(−c)·T(x,y)·Rx·Ry·Rz·K·S with CSS Transforms 2 matrices (+z toward the viewer, w = 1 − z/d).
    Without rx/ry it equals `affine_matrix(p)` exactly."""
    a, b = math.radians(p.get("rx", 0.0)), math.radians(p.get("ry", 0.0))
    Rx = np.array([[1.0, 0.0, 0.0], [0.0, math.cos(a), -math.sin(a)], [0.0, math.sin(a), math.cos(a)]])
    Ry = np.array([[math.cos(b), 0.0, math.sin(b)], [0.0, 1.0, 0.0], [-math.sin(b), 0.0, math.cos(b)]])
    M = affine_matrix(p)
    d, cx, cy = perspective_px(size), size[0] / 2, size[1] / 2
    # a 3D offset (u, v, z) from (x, y, 0) lands at ((x + u − cx·z/d) / w, (y + v − cy·z/d) / w), w = 1 − z/d
    P = np.array([[1.0, 0.0, -cx / d], [0.0, 1.0, -cy / d], [0.0, 0.0, -1.0 / d]])
    H = np.eye(3)
    H[:, :2] = P @ (Rx @ Ry)[:, :2] @ M[:2, :2]
    H[:2, 2] = M[:2, 2]
    return H


def decompose_affine(M: np.ndarray) -> dict[str, float]:
    a, b, c, d = M[0, 0], M[0, 1], M[1, 0], M[1, 1]
    rot = math.atan2(c, a)
    sx = math.hypot(a, c)
    R = np.array([[math.cos(rot), -math.sin(rot)], [math.sin(rot), math.cos(rot)]])
    U = R.T @ M[:2, :2]  # = [[sx, tan(skx)*sy],[0, sy]]
    sy = U[1, 1]
    skx = math.degrees(math.atan2(U[0, 1], sy)) if abs(sy) > 1e-12 else 0.0
    return {"x": float(M[0, 2]), "y": float(M[1, 2]), "sx": float(sx), "sy": float(sy),
            "rot": math.degrees(rot), "skx": float(skx), "sky": 0.0}


def local_corners(c: Canonical) -> np.ndarray:
    ax, ay = c.anchor
    x0, y0 = -ax * c.width, -ay * c.height
    return np.array([[x0, y0], [x0 + c.width, y0], [x0 + c.width, y0 + c.height], [x0, y0 + c.height]], dtype=float)


def element_corners(el: Element, f: float, size: tuple[int, int] | None = None) -> np.ndarray:
    """Scene corners of the canonical box; with the scene `size`, a tilted element's corners are projected."""
    p = eval_props(el, f)
    pts = np.c_[local_corners(el.canonical), np.ones(4)]
    if size is not None and is_tilted(p, el.pending_asset or el.kind):
        q = pts @ projective_matrix(p, size).T
        return q[:, :2] / q[:, 2:]
    M = affine_matrix(p)
    return (pts @ M.T)[:, :2]


def element_bbox(el: Element, f: float, size: tuple[int, int] | None = None) -> tuple[float, float, float, float]:
    # ponytail: captions, brief, solid_assets and verify.matrix call this without `size` and keep affine boxes,
    # so tilted titles get approximate boxes there; pass scene.size where a caller needs the projected box.
    P = element_corners(el, f, size)
    return float(P[:, 0].min()), float(P[:, 1].min()), float(P[:, 0].max()), float(P[:, 1].max())
