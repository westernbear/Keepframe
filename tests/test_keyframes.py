import numpy as np, pytest
from keepframe.ir.schema import Keyframe, Track, PROPS, DEFAULTS
from keepframe.ir.tracks import eval_track, PRESET_EASES
from keepframe.analyze.keyframes import reduce_curve, tracks_from_raw, fill_gaps, ERR

def dense(track, n):
    return np.array([eval_track(track, f) for f in range(n)])

def test_reduce_recovers_two_key_eased_segment():
    tr = Track(keys=[Keyframe(t=0, v=0.0, ease=PRESET_EASES["out_cubic"]), Keyframe(t=30, v=200.0)])
    keys, err = reduce_curve(dense(tr, 31), 0, 2.0)
    assert len(keys) <= 3 and err <= 2.0
    rec = Track(keys=keys)
    assert np.abs(dense(rec, 31) - dense(tr, 31)).max() <= 2.0
    assert keys[0].ease is not None

def test_reduce_splits_multi_segment():
    tr = Track(keys=[Keyframe(t=0, v=0.0), Keyframe(t=10, v=50.0, ease=PRESET_EASES["in_quad"]), Keyframe(t=40, v=0.0)])
    keys, err = reduce_curve(dense(tr, 41), 0, 2.0)
    assert 3 <= len(keys) <= 5 and err <= 2.0

def test_tracks_from_raw_skips_constant_defaults_and_reports_fit_error():
    n = 20
    raw = np.tile([[0, 0, 1, 1, 0, 0, 0, 1]], (n, 1)).astype(float)
    raw[:, 0] = np.linspace(100, 160, n)   # x moves; everything else default
    tracks, fe = tracks_from_raw(raw, first=5)
    assert set(tracks) == {"x"} and tracks["x"].keys[0].t == 5 and tracks["x"].keys[-1].t == 24
    assert fe.max_px <= 2.0

def test_tracks_from_raw_reports_longest_over_tolerance_run(monkeypatch):
    import keepframe.analyze.keyframes as keyframes

    raw = np.tile([0., 0., 1., 1., 0., 0., 0., 1.], (6, 1))
    raw[:, 0] = [0, 0, 5, 5, 0, 0]
    monkeypatch.setattr(keyframes, "reduce_curve", lambda values, first, tol: ([Keyframe(t=first, v=0)], 5.0))

    tracks, error = keyframes.tracks_from_raw(raw, first=10)

    assert tracks["x"].keys[0].t == 10
    assert error.max_px == 5.0
    assert error.max_frames == 2

def test_fill_gaps_interpolates_interior_only():
    raw = np.full((6, 8), np.nan); raw[1] = 0; raw[2] = np.nan; raw[3] = 2; raw[4] = 3
    out = fill_gaps(raw)
    assert np.isnan(out[0]).all() and np.isnan(out[5]).all() and out[2, 0] == pytest.approx(1.0)

def test_flat_noisy_stretch_is_a_hold():
    """V3: a move, then ±1 px of noise until the end. The stretch was one segment with an ease chosen by the noise and
    a last key at the last raw sample (a 1 px 'settle'); it is a hold: equal values, no ease."""
    rng = np.random.default_rng(0)
    tr = Track(keys=[Keyframe(t=0, v=0.0, ease=PRESET_EASES["out_cubic"]), Keyframe(t=12, v=248.0)])
    v = np.r_[dense(tr, 13), 248 + rng.uniform(-0.9, 0.9, 77)]
    v[-1] = 247.0
    keys, err = reduce_curve(v, 0, 2.0)
    assert err <= 2.0 and keys[-1].v == keys[-2].v and keys[-2].ease is None and keys[-1].t == 89
    assert np.abs(dense(Track(keys=keys), 90) - v).max() <= 2.0


def test_constant_track_is_one_key():
    rng = np.random.default_rng(1)
    keys, err = reduce_curve(0.5 + rng.uniform(-0.004, 0.004, 30), 7, 0.01)
    assert len(keys) == 1 and keys[0].t == 7 and keys[0].ease is None and err <= 0.01


def test_a_hold_does_not_break_the_move_after_it():
    """Snapping a hold's end shifts the next segment's start; a shift the move cannot absorb keeps the raw value."""
    v = np.r_[np.full(10, 100.0), 101.9, np.linspace(101.9, 300, 20)]
    keys, err = reduce_curve(v, 0, 2.0)
    assert err <= 2.0 and np.abs(dense(Track(keys=keys), len(v)) - v).max() <= 2.0
