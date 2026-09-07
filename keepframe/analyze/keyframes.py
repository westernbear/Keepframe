from __future__ import annotations
import numpy as np
from ..ir.schema import DEFAULTS, Ease, FitError, Keyframe, PROPS, Track
from ..ir.tracks import PRESET_EASES, bezier_y

ERR = {"x": 2.0, "y": 2.0, "sx": 0.01, "sy": 0.01, "rot": 1.0, "skx": 1.0, "sky": 1.0, "opacity": 0.02}


def _segment_error(values: np.ndarray, ease: Ease | None) -> float:
    n = len(values) - 1
    if n <= 0:
        return 0.0
    u = np.arange(n + 1) / n
    e = np.array([bezier_y(x, ease) for x in u]) if ease else u
    pred = values[0] + (values[-1] - values[0]) * e
    return float(np.abs(pred - values).max())


def fit_ease(values: np.ndarray) -> tuple[Ease | None, float]:
    best: tuple[Ease | None, float] = (None, _segment_error(values, None))
    for name, ease in PRESET_EASES.items():
        if name == "linear":
            continue
        err = _segment_error(values, ease)
        if err < best[1] - 1e-9:
            best = (ease, err)
    return best


def reduce_curve(values: np.ndarray, t0: int, max_err: float) -> tuple[list[Keyframe], float]:
    n = len(values)
    if n == 1:
        return [Keyframe(t=t0, v=float(values[0]))], 0.0

    def rec(a: int, b: int) -> tuple[list[tuple[int, float, Ease | None]], float]:
        seg = values[a:b + 1]
        ease, err = fit_ease(seg)
        if err <= max_err or b - a < 2:
            return [(a, float(values[a]), ease)], err
        u = np.arange(b - a + 1) / (b - a)
        e = np.array([bezier_y(x, ease) for x in u]) if ease else u
        pred = values[a] + (values[b] - values[a]) * e
        split = a + int(np.argmax(np.abs(pred - seg)[1:-1])) + 1
        left, el = rec(a, split)
        right, er = rec(split, b)
        return left + right, max(el, er)

    parts, err = rec(0, n - 1)
    keys = [Keyframe(t=t0 + a, v=v, ease=ease) for a, v, ease in parts]
    keys.append(Keyframe(t=t0 + n - 1, v=float(values[-1])))
    return keys, err


def fill_gaps(raw_full: np.ndarray) -> np.ndarray:
    out = raw_full.copy()
    valid = ~np.isnan(out[:, 0])
    if valid.sum() < 2:
        return out
    idx = np.flatnonzero(valid)
    for col in range(out.shape[1]):
        interior = np.arange(idx[0], idx[-1] + 1)
        out[interior, col] = np.interp(interior, idx, out[idx, col])
    return out


def tracks_from_raw(raw: np.ndarray, first: int) -> tuple[dict[str, Track], FitError]:
    tracks: dict[str, Track] = {}
    max_px, max_frames = 0.0, 0
    for i, prop in enumerate(PROPS):
        col = raw[:, i]
        if np.abs(col - DEFAULTS[prop]).max() <= ERR[prop]:
            continue
        keys, err = reduce_curve(col, first, ERR[prop])
        tracks[prop] = Track(keys=keys)
        if prop in ("x", "y"):
            max_px = max(max_px, err)
    return tracks, FitError(max_px=max_px, max_frames=max_frames)
