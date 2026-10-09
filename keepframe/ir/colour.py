"""Calibrated sRGB <-> CIELAB (D65, float math). ΔE here is CIE76."""
from __future__ import annotations
import numpy as np

_M = np.array([[0.4124564, 0.3575761, 0.1804375],
               [0.2126729, 0.7151522, 0.0721750],
               [0.0193339, 0.1191920, 0.9503041]])
_MI = np.linalg.inv(_M)
_WHITE = _M @ np.ones(3)
_EPS, _KAPPA = 216 / 24389, 24389 / 27


def _linear(c: np.ndarray) -> np.ndarray:
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


_LIN8 = _linear(np.arange(256, dtype=np.float64) / 255.0)


def srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    return _lin_to_lab(_linear(np.asarray(rgb, np.float64) / 255.0))


def srgb8_to_lab(rgb: np.ndarray) -> np.ndarray:
    """srgb_to_lab for uint8 input through a 256-entry linearisation table (hot loops)."""
    return _lin_to_lab(_LIN8[np.asarray(rgb, np.uint8)])


def _lin_to_lab(lin: np.ndarray) -> np.ndarray:
    t = (lin @ _M.T) / _WHITE
    f = np.where(t > _EPS, np.cbrt(t), (_KAPPA * t + 16) / 116)
    L = 116 * f[..., 1] - 16
    return np.stack([L, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1).astype(np.float32)


def lab_to_linear(lab: np.ndarray) -> np.ndarray:
    """Linear-light sRGB, unclipped: a channel outside 0..1 means the colour is out of the sRGB gamut."""
    lab = np.asarray(lab, np.float64)
    fy = (lab[..., 0] + 16) / 116
    f = np.stack([fy + lab[..., 1] / 500, fy, fy - lab[..., 2] / 200], -1)
    t = np.where(f ** 3 > _EPS, f ** 3, (116 * f - 16) / _KAPPA)
    return (t * _WHITE) @ _MI.T


def lab_to_srgb(lab: np.ndarray) -> np.ndarray:
    lin = np.clip(lab_to_linear(lab), 0.0, 1.0)
    c = np.where(lin <= 0.0031308, lin * 12.92, 1.055 * np.power(lin, 1 / 2.4) - 0.055)
    return np.clip(np.rint(c * 255.0), 0, 255).astype(np.uint8)


def delta_e(a, b) -> np.ndarray:
    return np.linalg.norm(np.asarray(a, np.float32) - np.asarray(b, np.float32), axis=-1)


def hex_to_rgb8(s: str) -> tuple[int, int, int]:
    s = s.lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def rgb8_to_hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(int(v) for v in rgb)
