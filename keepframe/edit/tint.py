"""Background tint (decision D5): move a background to a target colour's lightness and hue while keeping its
light/dark structure. In CIELAB, with the background's mean (L̄, ā, b̄) and lightness range [L_min, L_max]:

    L' = L_T + k·(L − L̄),  a' = a_T + (a − ā),  b' = b_T + (b − b̄),
    k  = min(1, L_T / max(L̄ − L_min, ε), (100 − L_T) / max(L_max − L̄, ε))

so L' stays in 0..100 and the mean lands on the target. A colour outside sRGB keeps L' and gives up chroma: first
its deviation from the target's (a, b), then, if needed, the target's own chroma (neutral grey always fits).
Pictures and posters become new assets (`assets/background.tint<n>.png`, never an existing name); gradients map
every stop and key through the same formula; video waits for Task 13 (TintError "tint_unavailable")."""
from __future__ import annotations

import os
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
GAMUT_STEPS = 16         # bisection steps of the chroma reduction
GAMUT_TOL = 1e-6
WORK_SIDE = 256          # long side of the renders gradient statistics are taken from
BALANCE_PX = 65536       # pixels the chroma balance looks at, at most
BALANCE_STEPS = 6
BALANCE_TOL = 0.25       # ΔE of the fitted mean (a, b) from the target's
BALANCE_MIN = 10.0       # the shift is bounded by max(this, the target's chroma)
MAX_PIXELS = 1 << 26     # a background larger than this is not tinted


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


def _fit_gamut(lab: np.ndarray, target: np.ndarray) -> np.ndarray:
    """Out-of-gamut colours: same L, chroma pulled toward the target's (a, b), then toward neutral."""
    bad = ~_in_gamut(lab)
    if not bad.any():
        return lab
    px = lab[bad]
    dev = px[:, 1:] - target[1:]

    def at(u: np.ndarray) -> np.ndarray:   # u 0..1: deviation shrinks; 1..2: the target's chroma shrinks
        u = u[:, None]
        ab = np.where(u <= 1, target[1:] + (1 - u) * dev, (2 - u) * target[1:])
        return np.concatenate([px[:, :1], ab], 1)

    lo, hi = np.zeros(len(px)), np.full(len(px), 2.0)
    for _ in range(GAMUT_STEPS):
        mid = (lo + hi) / 2
        inside = _in_gamut(at(mid))
        hi, lo = np.where(inside, mid, hi), np.where(inside, lo, mid)
    lab[bad] = at(hi)
    return lab


def tint_lab(lab: np.ndarray, target_lab, stats: TintStats) -> np.ndarray:
    """D5 on Lab values (any shape …×3), gamut-clipped; float64."""
    lab = np.array(lab, np.float64, copy=True)
    t = np.asarray(target_lab, np.float64).reshape(3)
    t = np.array([min(max(t[0], 0.0), 100.0), t[1], t[2]])
    k = _gain(stats, t[0])
    shape = lab.shape
    lab = lab.reshape(-1, 3)
    lab[:, 0] = np.clip(t[0] + k * (lab[:, 0] - stats.mean[0]), 0.0, 100.0)
    lab[:, 1] = t[1] + stats.shift[0] + (lab[:, 1] - stats.mean[1])
    lab[:, 2] = t[2] + stats.shift[1] + (lab[:, 2] - stats.mean[2])
    return _fit_gamut(lab, t).reshape(shape)


def balance(stats: TintStats, sample_lab: np.ndarray, target_lab, result=None) -> TintStats:
    """Dark or light pixels cannot hold the target's chroma, so the fitted mean (a, b) falls short of the
    target's; shift every pixel's (a, b) until the fitted mean of `sample_lab` is back on it (bounded).
    `result(stats) -> Lab pixels` measures something else instead (a gradient's render from mapped stops)."""
    t = np.asarray(target_lab, np.float64).reshape(3)
    sample = np.asarray(sample_lab, np.float64).reshape(-1, 3)
    if len(sample) > BALANCE_PX:
        sample = sample[np.linspace(0, len(sample) - 1, BALANCE_PX).astype(int)]
    result = result or (lambda s: tint_lab(sample, t, s))
    cap = max(BALANCE_MIN, float(np.hypot(t[1], t[2])))
    shift = np.zeros(2)
    for _ in range(BALANCE_STEPS):
        got = np.asarray(result(replace(stats, shift=tuple(shift))), np.float64).reshape(-1, 3)[:, 1:].mean(0)
        err = t[1:] - got
        if np.hypot(*err) < BALANCE_TOL:
            break
        shift = shift + err
        if np.hypot(*shift) > cap:
            shift *= cap / np.hypot(*shift)
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
    path = scene_asset_path(Path(scene_dir), rel)
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        raise TintError("tint_failed")
    if img.shape[0] * img.shape[1] > MAX_PIXELS:
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
            if bg.poster:
                try:
                    update["poster"] = _new_asset(scene_dir, tint_rgb(_read_rgb(scene_dir, bg.poster), target, stats))
                except (TintError, OSError, ValueError) as e:   # the gradient is the background; the still follows it
                    log.warning("gradient poster not tinted: %s", describe(e))
                    update["poster"] = None
            out = bg.model_copy(update=update)
    except TintError:
        raise
    except (OSError, ValueError, cv2.error) as e:
        log.warning("background tint failed: %s", describe(e))
        raise TintError("tint_failed") from None
    log.info("background tint %.2fs kind=%s", time.perf_counter() - t0, bg.kind)
    return out
