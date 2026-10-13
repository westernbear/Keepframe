from __future__ import annotations
import numpy as np
from ..ir.tracks import PRESET_EASES, bezier_y, eval_track
from ..ir.schema import DEFAULTS, Ease, FitError, Keyframe, PROPS, Track

ERR = {"x": 2.0, "y": 2.0, "sx": 0.01, "sy": 0.01, "rot": 1.0, "skx": 1.0, "sky": 1.0, "opacity": 0.02, "reveal": 0.01, "rx": 1.0, "ry": 1.0}


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

    parts, _ = rec(0, n - 1)
    keys = _holds(values, [list(p) for p in parts] + [[n - 1, float(values[-1]), None]], max_err)
    err = max(_fit(values, a, b, va, vb, ease) for (a, va, ease), (b, vb, _) in zip(keys, keys[1:])) if len(keys) > 1 \
        else float(np.abs(values - keys[0][1]).max())
    return [Keyframe(t=t0 + a, v=v, ease=ease) for a, v, ease in keys], err


def _fit(values: np.ndarray, a: int, b: int, va: float, vb: float, ease: Ease | None) -> float:
    """Largest |Δ| of the segment a→b drawn from va to vb with `ease` against the samples."""
    u = np.arange(b - a + 1) / max(b - a, 1)
    e = np.array([bezier_y(x, ease) for x in u]) if ease else u
    return float(np.abs(va + (vb - va) * e - values[a:b + 1]).max())


def _holds(values: np.ndarray, keys: list[list], max_err: float) -> list[list]:
    """Flat stretches as holds. A key pair whose samples all lie within max_err of its start value is a hold: its end
    takes the start value and it loses its ease (raw noise picked both, and they read as motion), unless the segment
    after it would then miss its samples by more than max_err. Equal runs keep their ends; a track with one value
    is one key."""
    for i in range(len(keys) - 1):
        (a, va, _), (b, _, eb) = keys[i], keys[i + 1]
        if np.abs(values[a:b + 1] - va).max() > max_err:
            continue
        if i + 2 < len(keys) and _fit(values, b, keys[i + 2][0], va, keys[i + 2][1], eb) > max_err:
            continue
        keys[i][2], keys[i + 1][1] = None, va
    out = [keys[0]]
    for prev, k, nxt in zip(keys, keys[1:], keys[2:]):
        if prev[1] == k[1] == nxt[1]:   # the middle of a hold
            continue
        out.append(k)
    out.append(keys[-1])
    for k, nxt in zip(out, out[1:]):
        if k[1] == nxt[1]:
            k[2] = None
    return [out[0][:2] + [None]] if all(k[1] == out[0][1] for k in out) else out


def fill_gaps(raw_full: np.ndarray) -> np.ndarray:
    out = raw_full.copy()
    for col in range(out.shape[1]):
        idx = np.flatnonzero(~np.isnan(out[:, col]))
        if len(idx) < 2:
            continue  # all-NaN columns remain absent/default
        interior = np.arange(idx[0], idx[-1] + 1)
        out[interior, col] = np.interp(interior, idx, out[idx, col])
    return out


def tracks_from_raw(raw: np.ndarray, first: int) -> tuple[dict[str, Track], FitError]:
    tracks: dict[str, Track] = {}
    max_px, max_frames = 0.0, 0
    over_tolerance = np.zeros(len(raw), dtype=bool)
    for i, prop in enumerate(PROPS[:raw.shape[1]]):
        col = raw[:, i]
        if np.isnan(col).all():
            continue
        if np.abs(col - DEFAULTS[prop]).max() <= ERR[prop]:
            continue
        keys, err = reduce_curve(col, first, ERR[prop])
        track = Track(keys=keys)
        tracks[prop] = track
        over_tolerance |= np.abs(np.array([eval_track(track, first + j) for j in range(len(col))]) - col) > ERR[prop]
        if prop in ("x", "y"):
            max_px = max(max_px, err)
    run = longest = 0
    for exceeds in over_tolerance:
        run = run + 1 if exceeds else 0
        longest = max(longest, run)
    return tracks, FitError(max_px=max_px, max_frames=longest)
