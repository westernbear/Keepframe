"""Gradients as CSS draws them: sRGB-encoded interpolation, same geometry in numpy and HTML."""
from __future__ import annotations
import math
import numpy as np
from .colour import hex_to_rgb8, rgb8_to_hex
from .schema import Background, Gradient, GradientStop


def gradient_t(g: Gradient, w: int, h: int, xy: tuple[np.ndarray, np.ndarray] | None = None) -> np.ndarray:
    """t at every pixel centre of a w×h frame, or at the frame coordinates xy = (x, y)."""
    if xy is None:
        ys, xs = np.mgrid[0:h, 0:w].astype(np.float64)
        xy = (xs + 0.5, ys + 0.5)
    x, y = xy
    if g.kind == "radial":
        r = g.radius * math.hypot(w, h) / 2
        return np.hypot(x - g.center[0] * w, y - g.center[1] * h) / r
    th = math.radians(g.angle)
    dx, dy = math.sin(th), -math.cos(th)
    length = abs(w * dx) + abs(h * dy)
    return ((x - w / 2) * dx + (y - h / 2) * dy) / length + 0.5


def render_gradient(g: Gradient, w: int, h: int, xy: tuple[np.ndarray, np.ndarray] | None = None) -> np.ndarray:
    t = np.clip(gradient_t(g, w, h, xy), 0.0, 1.0)
    offs = [s.offset for s in g.stops]
    cols = np.array([hex_to_rgb8(s.color) for s in g.stops], np.float64)
    out = np.stack([np.interp(t, offs, cols[:, c]) for c in range(3)], -1)
    return np.floor(out + 0.5).astype(np.uint8)


def _mix(a: Gradient, b: Gradient, u: float) -> Gradient:
    if a.kind != b.kind or len(a.stops) != len(b.stops):
        return a
    lerp = lambda p, q: p + (q - p) * u
    stops = [GradientStop(offset=lerp(p.offset, q.offset),
                          color=rgb8_to_hex([math.floor(lerp(i, j) + 0.5) for i, j in zip(hex_to_rgb8(p.color), hex_to_rgb8(q.color))]))
             for p, q in zip(a.stops, b.stops)]
    return Gradient(kind=a.kind, stops=stops, angle=lerp(a.angle, b.angle),
                    center=(lerp(a.center[0], b.center[0]), lerp(a.center[1], b.center[1])),
                    radius=lerp(a.radius, b.radius))


def gradient_at(bg: Background, f: int) -> Gradient:
    keys = bg.gradient_keys
    if not keys:
        return bg.gradient
    if f <= keys[0].t:
        return keys[0].gradient
    for a, b in zip(keys, keys[1:]):
        if f < b.t:
            return _mix(a.gradient, b.gradient, (f - a.t) / (b.t - a.t))
    return keys[-1].gradient


def _n(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s == "-0" else s


def gradient_css(g: Gradient, w: int, h: int) -> str:
    stops = ", ".join(f"{s.color} {_n(s.offset * 100)}%" for s in g.stops)
    if g.kind == "radial":
        r = g.radius * math.hypot(w, h) / 2
        return f"radial-gradient(circle {_n(r)}px at {_n(g.center[0] * 100)}% {_n(g.center[1] * 100)}%, {stops})"
    return f"linear-gradient({_n(g.angle)}deg, {stops})"
