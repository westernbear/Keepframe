"""Background tint (decision D5): move a background to a target colour's lightness and hue while keeping its
light/dark structure. In CIELAB, with the background's mean (L̄, ā, b̄) and lightness range [L_min, L_max]:

    L' = L_T + k·(L − L̄),  a' = a_T + (a − ā),  b' = b_T + (b − b̄),
    k  = min(1, L_T / max(L̄ − L_min, ε), (100 − L_T) / max(L_max − L̄, ε))

so L' stays in 0..100 and the mean lands on the target. A colour outside sRGB keeps L' and hue and gives up chroma
(toward neutral grey, which always fits; monotone, so smooth backgrounds stay smooth). Where that pulls the mean
(a, b) off the target, a shift of at most 10 ΔE turns hues back toward it without adding chroma (R53).
Pictures and posters become new assets (`assets/background.tint<n>.png`, never an existing name); gradients map
every stop and key through the same formula; video waits for Task 13 (TintError "tint_unavailable")."""
from __future__ import annotations

import functools
import os
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path

import cv2
import numpy as np

from ..ir.colour import hex_to_rgb8, lab_to_linear, lab_to_srgb, rgb8_to_hex, srgb8_to_lab, srgb_to_lab
from ..ir.gradient import render_gradient
from ..ir.paths import scene_asset_path
from ..ir.schema import Background, Gradient, GradientStop
from ..log import describe, get

log = get("keepframe.edit")
EPS = 1e-3
CHUNK = 1 << 20          # pixels converted at a time (bounded memory on 4K plates)
GAMUT_TOL = 1e-6
CAP_L_STEP, CAP_HUES = 0.5, 360          # chroma-limit grid: L every 0.5, hue every degree
CAP_C_STEP, CAP_C_MAX = 2.0, 160.0       # scan of the chroma limit (sRGB chroma stays under ~134)
CAP_LAMBDA_L = 2.5       # chroma per unit L the limit may change by: a step ΔL in moves ≤ √(1 + 2.5²)·ΔL ≈ 2.7·ΔL out
CAP_MU_H = 2.0           # log-chroma per radian of hue the limit may change by
CAP_MARGIN = 0.25        # chroma kept off the limit (bilinear interpolation between grid nodes)
WORK_SIDE = 256          # long side of the renders gradient statistics are taken from
BALANCE_PX = 65536       # pixels the chroma balance looks at, at most
BALANCE_STEPS = 6
BALANCE_TOL = 0.25       # ΔE of the fitted mean (a, b) from the target's
BALANCE_MAX = 10.0       # ΔE: the balance shift is a small correction only (R53)
MAX_PIXELS = 1 << 26     # a background larger than this is not tinted
_HEX = re.compile(r"#[0-9a-fA-F]{6}")


class TintError(ValueError):
    """The tint could not be made; `code` is what clients see (no exception text)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class TintStats:
    mean: tuple[float, float, float]
    l_min: float
    l_max: float
    shift: tuple[float, float] = (0.0, 0.0)   # (a, b) added back for chroma the gamut fit takes away (`balance`)


def tint_stats(lab: np.ndarray) -> TintStats:
    lab = np.asarray(lab, np.float64).reshape(-1, 3)
    m = lab.mean(0)
    return TintStats((float(m[0]), float(m[1]), float(m[2])), float(lab[:, 0].min()), float(lab[:, 0].max()))


def _pooled(parts: list[tuple[int, np.ndarray, float, float]]) -> TintStats:
    n = sum(p[0] for p in parts)
    m = sum(p[0] * p[1] for p in parts) / n
    return TintStats((float(m[0]), float(m[1]), float(m[2])), min(p[2] for p in parts), max(p[3] for p in parts))


def _gain(stats: TintStats, l_t: float) -> float:
    lm = stats.mean[0]
    return float(min(1.0, l_t / max(lm - stats.l_min, EPS), (100.0 - l_t) / max(stats.l_max - lm, EPS)))


def _in_gamut(lab: np.ndarray) -> np.ndarray:
    lin = lab_to_linear(lab)
    return ((lin >= -GAMUT_TOL) & (lin <= 1 + GAMUT_TOL)).all(-1)


def _envelope(cap: np.ndarray) -> np.ndarray:
    """The largest function under `cap` that changes by at most CAP_LAMBDA_L chroma per unit L and CAP_MU_H in
    log-chroma per radian of hue (min-plus passes; hue wraps)."""
    cap = cap.copy()
    dl, dh = CAP_LAMBDA_L * CAP_L_STEP, CAP_MU_H * 2 * np.pi / CAP_HUES
    for _ in range(2):
        for i in range(1, len(cap)):
            cap[i] = np.minimum(cap[i], cap[i - 1] + dl)
        for i in range(len(cap) - 2, -1, -1):
            cap[i] = np.minimum(cap[i], cap[i + 1] + dl)
        lc = np.log(np.maximum(cap, 1e-6))
        for _ in range(2):
            for j in range(CAP_HUES):
                lc[:, j] = np.minimum(lc[:, j], lc[:, j - 1] + dh)
            for j in range(CAP_HUES - 1, -1, -1):
                lc[:, j] = np.minimum(lc[:, j], lc[:, (j + 1) % CAP_HUES] + dh)
        cap = np.exp(lc)
    return cap


@functools.lru_cache(maxsize=1)
def _cap_table() -> np.ndarray:
    """Chroma limit per (L, hue) grid node. Raw: the first sRGB boundary seen from neutral grey along the hue
    (scan, then bisection). Clipping to that boundary would turn smooth backgrounds into steps: it is steep near
    black (CIELAB's linear segment gives ~20 chroma per unit L toward blue) and dented near the yellow cusp above
    L≈94 (a ray from grey leaves sRGB and re-enters it). The Lipschitz envelope (`_envelope`) removes both."""
    t0 = time.perf_counter()
    ls = np.arange(0, 100 + 1e-9, CAP_L_STEP)
    hs = np.arange(CAP_HUES) * 2 * np.pi / CAP_HUES
    cs = np.arange(0, CAP_C_MAX + 1e-9, CAP_C_STEP)
    ca, sa = np.cos(hs), np.sin(hs)
    raw = np.zeros((len(ls), CAP_HUES))
    for i, L in enumerate(ls):
        inside = _in_gamut(np.stack([np.full((CAP_HUES, len(cs)), L), ca[:, None] * cs, sa[:, None] * cs], -1))
        first = np.where((~inside).any(1), np.argmax(~inside, 1), len(cs))
        lo = cs[np.maximum(first - 1, 0)]
        hi = cs[np.minimum(first, len(cs) - 1)]
        for _ in range(10):
            mid = (lo + hi) / 2
            ok = _in_gamut(np.stack([np.full(CAP_HUES, L), ca * mid, sa * mid], -1))
            lo, hi = np.where(ok, mid, lo), np.where(ok, hi, mid)
        raw[i] = np.where(first < len(cs), lo, CAP_C_MAX)
    cap = _envelope(raw)
    log.info("tint gamut table %.2fs", time.perf_counter() - t0)
    return cap


def _cap(L: np.ndarray, h: np.ndarray) -> np.ndarray:
    """Bilinear lookup of the chroma limit."""
    table = _cap_table()
    fi = np.clip(L / CAP_L_STEP, 0, len(table) - 1 - 1e-9)
    i0 = np.floor(fi).astype(int)
    wi = fi - i0
    fj = np.mod(h, 2 * np.pi) / (2 * np.pi / CAP_HUES)
    j0 = np.floor(fj).astype(int) % CAP_HUES
    wj = fj - np.floor(fj)
    j1 = (j0 + 1) % CAP_HUES
    top = (1 - wj) * table[i0, j0] + wj * table[i0, j1]
    bottom = (1 - wj) * table[i0 + 1, j0] + wj * table[i0 + 1, j1]
    return np.maximum((1 - wi) * top + wi * bottom - CAP_MARGIN, 0.0)


def _fit_gamut(lab: np.ndarray) -> np.ndarray:
    """Colours past the chroma limit keep L and hue and drop to it (toward neutral grey). The limit is continuous
    and Lipschitz in L and hue, so neighbouring colours stay neighbours (no false edges); it lies inside sRGB up to
    the grid's interpolation (the 8-bit conversion clips that remainder)."""
    c = np.hypot(lab[:, 1], lab[:, 2])
    limit = _cap(lab[:, 0], np.arctan2(lab[:, 2], lab[:, 1]))
    over = c > limit
    lab[over, 1:] *= (limit[over] / c[over])[:, None]
    return lab


def _d5(lab: np.ndarray, t: np.ndarray, stats: TintStats, shift=(0.0, 0.0)) -> np.ndarray:
    k = _gain(stats, t[0])
    out = np.empty_like(lab)
    out[:, 0] = np.clip(t[0] + k * (lab[:, 0] - stats.mean[0]), 0.0, 100.0)
    out[:, 1] = t[1] + shift[0] + (lab[:, 1] - stats.mean[1])
    out[:, 2] = t[2] + shift[1] + (lab[:, 2] - stats.mean[2])
    return out


def tint_lab(lab: np.ndarray, target_lab, stats: TintStats) -> np.ndarray:
    """D5 on Lab values (any shape …×3), gamut-fitted; float64. With a `balance` shift, no colour gets more chroma
    than pure D5 gives it (R53): the shift only turns hues toward the target's."""
    lab = np.asarray(lab, np.float64)
    t = np.asarray(target_lab, np.float64).reshape(3)
    t = np.array([min(max(t[0], 0.0), 100.0), t[1], t[2]])
    shape = lab.shape
    lab = lab.reshape(-1, 3)
    pure = _fit_gamut(_d5(lab, t, stats))
    if not any(stats.shift):
        return pure.reshape(shape)
    out = _fit_gamut(_d5(lab, t, stats, stats.shift))
    cp, co = np.hypot(pure[:, 1], pure[:, 2]), np.hypot(out[:, 1], out[:, 2])
    over = co > cp
    out[over, 1:] *= (cp[over] / co[over])[:, None]
    return out.reshape(shape)


def balance(stats: TintStats, sample_lab: np.ndarray, target_lab, result=None) -> TintStats:
    """A small correction (R53): when the fitted mean (a, b) misses the target's, shift every colour's (a, b) by
    at most BALANCE_MAX ΔE toward closing the gap; `tint_lab` never lets the shift raise a colour's chroma above
    pure D5's. `result(stats) -> Lab pixels` measures something else instead (a gradient's render from mapped
    stops)."""
    t = np.asarray(target_lab, np.float64).reshape(3)
    sample = np.asarray(sample_lab, np.float64).reshape(-1, 3)
    if len(sample) > BALANCE_PX:
        sample = sample[np.linspace(0, len(sample) - 1, BALANCE_PX).astype(int)]
    result = result or (lambda s: tint_lab(sample, t, s))
    shift = np.zeros(2)
    for _ in range(BALANCE_STEPS):
        got = np.asarray(result(replace(stats, shift=tuple(shift))), np.float64).reshape(-1, 3)[:, 1:].mean(0)
        err = t[1:] - got
        if np.hypot(*err) < BALANCE_TOL:
            break
        shift = shift + err
        if np.hypot(*shift) > BALANCE_MAX:
            shift *= BALANCE_MAX / np.hypot(*shift)
    return replace(stats, shift=(float(shift[0]), float(shift[1])))


def image_stats(rgb: np.ndarray) -> TintStats:
    flat = np.asarray(rgb, np.uint8).reshape(-1, 3)
    parts = []
    for i in range(0, len(flat), CHUNK):
        lab = srgb8_to_lab(flat[i:i + CHUNK]).astype(np.float64)
        parts.append((len(lab), lab.mean(0), float(lab[:, 0].min()), float(lab[:, 0].max())))
    return _pooled(parts)


def tint_rgb(rgb: np.ndarray, target_lab, stats: TintStats) -> np.ndarray:
    """D5 on an 8-bit RGB image (H×W×3), in chunks."""
    rgb = np.asarray(rgb, np.uint8)
    flat = rgb.reshape(-1, 3)
    out = np.empty_like(flat)
    for i in range(0, len(flat), CHUNK):
        out[i:i + CHUNK] = lab_to_srgb(tint_lab(srgb8_to_lab(flat[i:i + CHUNK]), target_lab, stats))
    return out.reshape(rgb.shape)


def _work_size(size: tuple[int, int] | None) -> tuple[int, int]:
    w, h = size or (16, 9)
    s = WORK_SIDE / max(w, h, 1)
    return max(2, round(w * s)), max(2, round(h * s))


def _gradients(bg: Background) -> list[Gradient]:
    return ([bg.gradient] if bg.gradient is not None else []) + [k.gradient for k in bg.gradient_keys]


def _read_rgb(scene_dir: Path, rel: str) -> np.ndarray:
    from PIL import Image
    path = scene_asset_path(Path(scene_dir), rel)
    with Image.open(path) as im:   # the header first: a huge picture is refused before it is decoded
        if im.size[0] * im.size[1] > MAX_PIXELS:
            raise TintError("tint_failed")
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        raise TintError("tint_failed")
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def _sample_lab(rgb: np.ndarray) -> np.ndarray:
    flat = np.asarray(rgb, np.uint8).reshape(-1, 3)
    if len(flat) > BALANCE_PX:
        flat = flat[np.linspace(0, len(flat) - 1, BALANCE_PX).astype(int)]
    return srgb8_to_lab(flat).astype(np.float64)


def _gradient_rgb(bg: Background, size: tuple[int, int] | None) -> np.ndarray:
    w, h = _work_size(size)
    return np.concatenate([render_gradient(g, w, h).reshape(-1, 3) for g in _gradients(bg)])


def background_stats(bg: Background, scene_dir: Path, size: tuple[int, int] | None = None, *,
                     target_hex: str | None = None) -> TintStats:
    """The statistics D5 takes from a background: the picture's (or the poster's) pixels, a gradient's renders
    (every key pooled, so all keys share one mapping), a flat colour's own value; balanced for `target_hex`."""
    if bg.kind == "color":
        lab = srgb_to_lab(np.float32(hex_to_rgb8(bg.value))).astype(np.float64)
        return TintStats((float(lab[0]), float(lab[1]), float(lab[2])), float(lab[0]), float(lab[0]))
    rgb = _gradient_rgb(bg, size) if bg.kind == "gradient" else _read_rgb(scene_dir, bg.value if bg.kind == "image" else bg.poster or "")
    stats = image_stats(rgb)
    if target_hex is None:
        return stats
    target = srgb_to_lab(np.float32(hex_to_rgb8(target_hex)))
    if bg.kind != "gradient":
        return balance(stats, _sample_lab(rgb), target)
    w, h = _work_size(size)   # gradients interpolate their mapped stops in sRGB: balance what they render
    rendered = lambda s: srgb8_to_lab(np.concatenate([render_gradient(_tint_gradient(g, target, s), w, h).reshape(-1, 3)
                                                      for g in _gradients(bg)]))
    return balance(stats, np.zeros((1, 3)), target, rendered)


def _new_asset(scene_dir: Path, rgb: np.ndarray) -> str:
    """Writes `assets/background.tint<n>.png` under the first unused n (exclusive create: never overwrites)."""
    ok, png = cv2.imencode(".png", cv2.cvtColor(np.ascontiguousarray(rgb), cv2.COLOR_RGB2BGR))
    if not ok:
        raise TintError("tint_failed")
    assets = Path(scene_dir) / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        path = assets / f"background.tint{n}.png"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            n += 1
            continue
        with os.fdopen(fd, "wb") as f:
            f.write(png.tobytes())
        return f"assets/{path.name}"


def _tint_hex(value: str, target: np.ndarray, stats: TintStats) -> str:
    return rgb8_to_hex(lab_to_srgb(tint_lab(srgb_to_lab(np.float32(hex_to_rgb8(value))), target, stats)))


def _tint_gradient(g: Gradient, target: np.ndarray, stats: TintStats) -> Gradient:
    lab = srgb_to_lab(np.float32([hex_to_rgb8(s.color) for s in g.stops]))
    rgb = lab_to_srgb(tint_lab(lab, target, stats))
    return g.model_copy(update={"stops": [GradientStop(offset=s.offset, color=rgb8_to_hex(c)) for s, c in zip(g.stops, rgb)]})


def tint_background(bg: Background, scene_dir: Path, target_hex: str, *, size: tuple[int, int] | None = None) -> Background:
    """The background moved to `target_hex` by D5. Raises TintError (code) when it cannot be made; nothing is
    written then. `size`: the scene size (gradient geometry for the statistics)."""
    t0 = time.perf_counter()
    target = srgb_to_lab(np.float32(hex_to_rgb8(target_hex))).astype(np.float64)
    if bg.kind == "color":
        return Background(kind="color", value=target_hex, confidence=1.0)
    if bg.kind == "video":   # ponytail: per-frame re-encode lands with Task 13's video plates
        raise TintError("tint_unavailable")
    try:
        if bg.kind == "image":
            rgb = _read_rgb(scene_dir, bg.value)
            stats = balance(image_stats(rgb), _sample_lab(rgb), target)
            out = bg.model_copy(update={"value": _new_asset(scene_dir, tint_rgb(rgb, target, stats))})
        else:
            stats = background_stats(bg, scene_dir, size, target_hex=target_hex)
            update = {"gradient": _tint_gradient(bg.gradient, target, stats) if bg.gradient is not None else None,
                      "gradient_keys": [k.model_copy(update={"gradient": _tint_gradient(k.gradient, target, stats)})
                                        for k in bg.gradient_keys]}
            if _HEX.fullmatch(bg.value or ""):   # the representative colour (AE's flat fallback without a poster)
                update["value"] = _tint_hex(bg.value, target, stats)
            if bg.poster:
                try:
                    update["poster"] = _new_asset(scene_dir, tint_rgb(_read_rgb(scene_dir, bg.poster), target, stats))
                except Exception as e:   # the gradient is the background; the still follows it
                    log.warning("gradient poster not tinted: %s", describe(e))
                    update["poster"] = None
            out = bg.model_copy(update=update)
    except TintError:
        raise
    except Exception as e:   # fail soft: no tint, nothing written, a code for the client
        log.warning("background tint failed: %s", describe(e))
        raise TintError("tint_failed") from None
    log.info("background tint %.2fs kind=%s", time.perf_counter() - t0, bg.kind)
    return out
