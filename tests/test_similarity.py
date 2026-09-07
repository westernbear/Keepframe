import numpy as np, pytest
from keepframe.ir.synth import make_synthetic_scene
from keepframe.verify.similarity import centroid_tracks, tracklet_correlation, temporal_similarity, appearance_similarity, frame_l1

def test_identical_scene_scores_one(tmp_scene_dir):
    s = make_synthetic_scene(tmp_scene_dir, seed=4)
    c = centroid_tracks(s)
    assert temporal_similarity(c, c) == pytest.approx(1.0, abs=1e-6)

def test_reversed_motion_scores_low():
    t = np.linspace(0, 100, 30)
    a = np.c_[t, np.zeros_like(t)]
    b = np.c_[t[::-1], np.zeros_like(t)]
    assert tracklet_correlation(a, a) == pytest.approx(1.0)
    assert tracklet_correlation(a, b) < 0.0
    assert temporal_similarity({"e": a}, {"e": b}) == 0.0   # clamped

def test_static_vs_moving_is_zero_and_speed_ratio_counts():
    t = np.linspace(0, 100, 30)
    moving = np.c_[t, np.zeros_like(t)]
    static = np.zeros_like(moving)
    assert tracklet_correlation(moving, static) == pytest.approx(0.0)
    half = np.c_[t / 2, np.zeros_like(t)]
    assert tracklet_correlation(moving, half) == pytest.approx(0.5)

def test_appearance_and_frame_l1():
    a = np.zeros((10, 10, 4), np.float32); a[..., 3] = 1; a[..., 0] = 1
    b = a.copy(); b[..., 0] = 0.5
    assert appearance_similarity(a, a) == pytest.approx(1.0)
    assert appearance_similarity(a, b) == pytest.approx(1 - 0.5 / 4)
    assert frame_l1(np.zeros((2, 2, 3)), np.ones((2, 2, 3))) == 1.0
