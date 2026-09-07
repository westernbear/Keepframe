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

def test_fill_gaps_interpolates_interior_only():
    raw = np.full((6, 8), np.nan); raw[1] = 0; raw[2] = np.nan; raw[3] = 2; raw[4] = 3
    out = fill_gaps(raw)
    assert np.isnan(out[0]).all() and np.isnan(out[5]).all() and out[2, 0] == pytest.approx(1.0)
