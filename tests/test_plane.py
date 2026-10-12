"""Task 8 (Stage B): text plane primitives (quad, rectify, ECC homography tracking, props fit, smoothing) on known warps."""
import time

import cv2
import numpy as np

from keepframe.analyze.plane import contrast, fit_props, plane_raw, quad_tilted, rectify, text_quad, track_plane
from keepframe.ir.schema import DEFAULTS, PROPS, FontGuess
from keepframe.ir.tracks import projective_matrix

SIZE = (1280, 720)


def _alpha(text: str, px: int = 64) -> np.ndarray:
    """The title's α, padded to even dims so an untilted copy sits on whole pixels (as synth's plane title)."""
    from keepframe.fonts.raster import render_styled
    a = render_styled([text], FontGuess(family_guess="Inter", weight=700, size_px=px, source="bundled"), "#ffffff", None)[..., 3]
    return np.pad(a, ((0, a.shape[0] % 2), (0, a.shape[1] % 2)))


def _H(p: dict, wh, size=SIZE) -> np.ndarray:
    """Texture px → scene px of a canonical box `wh` (anchor at its centre), as the composite draws it."""
    return projective_matrix(dict(DEFAULTS, **p), size) @ np.array([[1.0, 0, -wh[0] / 2], [0, 1, -wh[1] / 2], [0, 0, 1]])


def _apply(H: np.ndarray, pts) -> np.ndarray:
    q = np.c_[np.asarray(pts, float), np.ones(len(pts))] @ np.asarray(H, float).T
    return q[:, :2] / q[:, 2:]


def _box(w, h):
    return [[0, 0], [w, 0], [w, h], [0, h]]


def _plate(size) -> np.ndarray:
    W, H = size
    x, y = np.meshgrid(np.linspace(0, 1, W), np.linspace(0, 1, H))
    return np.dstack([40 + 60 * x, 30 + 50 * y, 90 - 40 * x]).astype(np.uint8)


def _turning(size=(640, 360), px=64, n=24, occluded=()):
    """A yellow title turning in yaw (−35° → −18°) and pitch (0 → 6°) while it drifts, over a gradient plate; an
    opaque card covers it on the `occluded` frames. Returns (frames, plate, true texture→scene H per frame, wh)."""
    a = _alpha("TURNING TITLE", px).astype(np.float32) / 255
    wh = (a.shape[1], a.shape[0])
    plate = _plate(size)
    frames, truth = [], []
    for f in range(n):
        u = f / (n - 1)
        H = _H(dict(x=size[0] * (0.48 + 0.04 * u), y=size[1] * (0.47 - 0.03 * u), rot=2.0, ry=-35 + 17 * u, rx=6 * u), wh, size)
        al = cv2.warpPerspective(a, H, size)[..., None]
        img = plate * (1 - al) + np.array([250, 220, 60]) * al
        if f in occluded:
            q = _apply(H, _box(*wh))
            x0, y0 = np.floor(q.min(0)).astype(int) - 4
            x1, y1 = np.ceil(q.max(0)).astype(int) + 4
            img[max(y0, 0):y1, max(x0, 0):x1] = (200, 40, 160)
        frames.append(img.round().astype(np.uint8))
        truth.append(H)
    return np.array(frames), plate, truth, wh


def _setup(frames, plate, cf):
    """What the merge does at the settled frame: the glyph mask, its quad, the rectified contrast template."""
    c = contrast(frames[cf], plate)
    quad, _ = text_quad(c > 60)
    tpl, H0 = rectify(c, quad)
    return tpl, H0


def _track_errors(Hs, truth, H0, tpl, cf):
    """Per frame, the largest corner distance (px) between the tracked patch quad and the true one."""
    G = np.linalg.inv(truth[cf]) @ H0          # patch px → texture px, fixed by the plane at cf
    box = _box(tpl.shape[1], tpl.shape[0])
    return {f: float(np.linalg.norm(_apply(H, box) - _apply(truth[f] @ G, box), axis=1).max()) for f, H in Hs.items()}


def test_fit_props_inverts_projective_matrix():
    rng = np.random.default_rng(8)
    for _ in range(200):
        wh = (int(rng.integers(80, 600)), int(rng.integers(30, 160)))
        p = dict(x=rng.uniform(150, 1130), y=rng.uniform(100, 620), sx=rng.uniform(0.5, 2), sy=rng.uniform(0.5, 2),
                 rot=rng.uniform(-30, 30), skx=rng.uniform(-10, 10), rx=rng.uniform(-40, 40), ry=rng.uniform(-40, 40))
        got, err = fit_props(_H(p, wh), wh, SIZE)
        residual = np.linalg.norm(_apply(_H(got, wh), _box(*wh)) - _apply(_H(p, wh), _box(*wh)), axis=1).max()
        assert residual <= 0.05 and err <= 0.05, (p, got, residual)
        assert max(abs(got[k] - p[k]) for k in ("rot", "skx", "rx", "ry")) <= 0.2, (p, got)


def test_text_quad_on_upright_lowercase_title_is_untilted():
    a = _alpha("playful typography, quickly jumped") > 127
    mask = np.zeros(SIZE[::-1], bool)
    mask[300:300 + a.shape[0], 150:150 + a.shape[1]] = a
    quad, info = text_quad(mask)
    assert abs(info["rot_deg"]) < 0.5 and abs(info["converge_deg"]) < 0.5, info
    assert not quad_tilted(info)
    assert quad[:, 0].min() < 150 + np.flatnonzero(a.any(0))[0] and quad[:, 0].max() > 150 + np.flatnonzero(a.any(0))[-1]


def test_text_quad_on_yawed_title():
    """Seed 10's yaw, its baseline near the stage's height (the perpendicular sides' exact case)."""
    a = _alpha("YAWED TITLE")
    size, wh = (640, 360), (a.shape[1], a.shape[0])
    H = _H(dict(x=320, y=170, ry=-30), wh, size)
    mask = cv2.warpPerspective(a.astype(np.float32) / 255, H, size) > 0.5
    # the truth: the canonical cap line (p10 of the column tops), baseline (median bottom) and ink extremes,
    # padded 0.1·h left/right and 0.35·h top/bottom, through H
    ink = a > 127
    cols = np.flatnonzero(ink.any(0))
    top = np.percentile(ink[:, cols].argmax(0) - 0.5, 10)
    bot = np.median(len(ink) - ink[::-1][:, cols].argmax(0) - 0.5)
    left, right = cols[0] - 0.5, cols[-1] + 0.5
    h = bot - top
    want = _apply(H, [[left - 0.1 * h, top - 0.35 * h], [right + 0.1 * h, top - 0.35 * h],
                      [right + 0.1 * h, bot + 0.35 * h], [left - 0.1 * h, bot + 0.35 * h]])
    quad, info = text_quad(mask)
    assert np.linalg.norm(quad - want, axis=1).max() <= 2.0, (quad, want)
    assert quad_tilted(info), info


def test_rectify_round_trip():
    a = _alpha("Round Trip", 72).astype(np.float32)[..., None] / 255
    tex = (a * [240, 200, 40] + (1 - a) * [30, 60, 120]).astype(np.float32)
    wh = (tex.shape[1], tex.shape[0])
    H = _H(dict(x=640, y=360, sx=1.3, sy=1.2, rot=-8, skx=4, rx=15, ry=25), wh)
    scene = cv2.warpPerspective(tex, H, SIZE)
    patch, H2 = rectify(scene, _apply(H, _box(*wh)), wh)
    assert patch.shape == tex.shape and np.abs(_apply(H2, _box(*wh)) - _apply(H, _box(*wh))).max() <= 1e-3
    assert np.abs(patch - tex)[2:-2, 2:-2].mean() <= 4.0


def test_track_plane_follows_a_turning_title():
    frames, plate, truth, _ = _turning()
    cf = 16
    tpl, H0 = _setup(frames, plate, cf)
    Hs, held = track_plane(frames, plate, tpl, H0, cf, (0, len(frames) - 1))
    assert sorted(Hs) == list(range(len(frames))) and not held
    err = np.array(list(_track_errors(Hs, truth, H0, tpl, cf).values()))
    assert np.median(err) <= 1.5 and err.max() <= 4.0, err.round(2)


def test_track_plane_holds_through_an_occluder():
    frames, plate, truth, _ = _turning(occluded=(8, 9, 10))
    cf = 16
    tpl, H0 = _setup(frames, plate, cf)
    Hs, held = track_plane(frames, plate, tpl, H0, cf, (0, len(frames) - 1))
    assert held == {8, 9, 10}
    assert all(np.array_equal(Hs[f], Hs[11]) for f in held)   # held from the side of cf
    err = _track_errors(Hs, truth, H0, tpl, cf)
    assert max(e for f, e in err.items() if f not in held) <= 4.0, err


def test_plane_raw_smooths_and_keeps_nan_outside():
    rng = np.random.default_rng(5)
    wh, n = (300, 80), 44
    smooth, five, short = range(4, 24), range(28, 33), range(37, 40)
    truth = {f: dict(x=300 + 4.0 * f, y=200 + 1.0 * f, sx=1.1, sy=0.9, rot=0.5 * f, skx=2.0, rx=5.0, ry=-30 + 0.5 * f)
             for f in [*smooth, *five, *short]}
    noisy = {f: dict(p, x=p["x"] + rng.normal(0, 1.5), y=p["y"] + rng.normal(0, 1.5), ry=p["ry"] + rng.normal(0, 1.0))
             for f, p in truth.items()}
    raw = plane_raw({f: _H(p, wh) for f, p in noisy.items()}, wh, SIZE, n, 0.8)
    assert raw.shape == (n, 11)
    assert np.isnan(raw[[f for f in range(n) if f not in truth]]).all()
    assert np.isnan(raw[:, PROPS.index("reveal")]).all()
    inside = sorted(truth)
    assert (raw[inside, PROPS.index("opacity")] == 0.8).all() and (raw[inside, PROPS.index("sky")] == 0).all()
    assert np.isfinite(np.delete(raw[inside], PROPS.index("reveal"), axis=1)).all()
    for k in ("x", "y", "ry"):
        col, fr = raw[:, PROPS.index(k)], list(smooth)
        fitted = np.array([noisy[f][k] for f in fr])
        assert np.abs(np.diff(col[fr], 2)).mean() < 0.4 * np.abs(np.diff(fitted, 2)).mean(), k   # smoothed
        assert np.abs(col[fr] - [truth[f][k] for f in fr]).mean() < np.abs(fitted - [truth[f][k] for f in fr]).mean(), k
        assert np.allclose(col[list(short)], [noisy[f][k] for f in short], atol=1e-3), k   # < 5 frames: as fitted


def test_track_plane_speed():
    frames, plate, truth, _ = _turning(size=(1920, 1080), px=150, n=31)
    cf = 30
    tpl, H0 = _setup(frames, plate, cf)
    assert max(tpl.shape) > 384                     # the 384 px working scale applies
    t0 = time.perf_counter()
    Hs, held = track_plane(frames, plate, tpl, H0, cf, (0, cf), work=384)
    per_frame = (time.perf_counter() - t0) / cf
    assert not held and per_frame <= 0.025, per_frame
