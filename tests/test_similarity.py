import numpy as np, pytest
from keepframe.ir.synth import make_synthetic_scene
from keepframe.ir.schema import Background, Canonical, Element, Keyframe, Scene, Track
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


def _fragmented_scene(two_frame_elements=20):
    elements = [Element(id=f"single{i}", kind="sprite", canonical=Canonical(width=10, height=10),
                        visible=(i % 10, i % 10)) for i in range(20)]
    elements.extend(Element(id=f"double{i}", kind="sprite", canonical=Canonical(width=10, height=10),
                            visible=(0, 1)) for i in range(two_frame_elements))
    for i in range(3):
        start, end = 10 + i * 10, 19 + i * 10
        elements.append(Element(id=f"moving{i}", kind="sprite", canonical=Canonical(width=10, height=10),
                                visible=(start, end), tracks={"x": Track(keys=[
                                    Keyframe(t=start, v=0), Keyframe(t=end, v=90)])}))
    return Scene(id="fragments", size=(200, 100), fps=30, frames=40,
                 background=Background(), elements=elements)


def test_identical_fragmented_scenes_score_one():
    scene = _fragmented_scene()
    ref = centroid_tracks(scene)
    out = centroid_tracks(scene.model_copy(deep=True))
    assert temporal_similarity(ref, out) == 1.0


def test_reversed_motion_in_fragmented_scene_still_fails_gate():
    scene = _fragmented_scene(two_frame_elements=0)
    out = scene.model_copy(deep=True)
    keys = out.element("moving0").tracks["x"].keys
    keys[0].v, keys[-1].v = keys[-1].v, keys[0].v
    score = temporal_similarity(centroid_tracks(scene), centroid_tracks(out))
    assert score == pytest.approx(1 / 3)
    assert score < 0.7


@pytest.mark.parametrize("a, b", [
    (np.empty((0, 2)), np.empty((0, 2))),
    (np.array([[1., 2.]]), np.array([[1., 2.]])),
    (np.full((3, 2), np.nan), np.zeros((3, 2))),
    (np.array([[1., 2.], [np.nan, np.nan], [3., 4.]]),
     np.array([[1., 2.], [np.nan, np.nan], [3., 4.]])),
    (np.array([[1., 2.], [3., 4.], [np.nan, np.nan]]),
     np.array([[np.nan, np.nan], [1., 2.], [3., 4.]])),
])
def test_tracklets_without_overlapping_diffs_have_no_evidence(a, b):
    assert tracklet_correlation(a, b) is None


def test_identical_static_tracks_score_one():
    ref = {"a": np.zeros((4, 2)), "b": np.full((4, 2), 20.)}
    assert temporal_similarity(ref, {key: track.copy() for key, track in ref.items()}) == 1.0


def test_identical_tracks_without_motion_evidence_score_one():
    scene = _fragmented_scene(two_frame_elements=0)
    scene.elements = [el for el in scene.elements if el.id.startswith("single")]
    ref = centroid_tracks(scene)
    out = {key: track.copy() for key, track in reversed(list(ref.items()))}
    score = temporal_similarity(ref, out)
    assert score == 1.0 and isinstance(score, float)


@pytest.mark.parametrize("change", ["position", "visibility", "shape", "key"])
def test_changed_tracks_without_motion_evidence_score_zero(change):
    ref = {"fragment": np.array([[np.nan, np.nan], [10., 20.], [np.nan, np.nan]])}
    out = {key: track.copy() for key, track in ref.items()}
    if change == "position":
        out["fragment"][1, 0] += 1
    elif change == "visibility":
        out["fragment"] = np.roll(out["fragment"], 1, axis=0)
    elif change == "shape":
        out["fragment"] = out["fragment"][:2]
    else:
        out["other"] = out.pop("fragment")
    assert temporal_similarity(ref, out) == 0.0


def test_unmatched_fragments_are_skipped_in_both_directions():
    moving = np.c_[np.arange(4.), np.zeros(4)]
    ref = {"moving": moving, "fragment": np.array([[1., 2.]])}
    out = {"moving": moving.copy()}
    assert temporal_similarity(ref, out) == 1.0
    assert temporal_similarity(out, ref) == 1.0


@pytest.mark.parametrize("ref, out, expected", [({}, {}, 1.0), ({"e": np.zeros((2, 2))}, {}, 0.0)])
def test_empty_track_dicts_without_evidence(ref, out, expected):
    assert temporal_similarity(ref, out) == expected
    assert temporal_similarity(out, ref) == expected

def test_appearance_and_frame_l1():
    a = np.zeros((10, 10, 4), np.float32); a[..., 3] = 1; a[..., 0] = 1
    b = a.copy(); b[..., 0] = 0.5
    assert appearance_similarity(a, a) == pytest.approx(1.0)
    assert appearance_similarity(a, b) == pytest.approx(1 - 0.5 / 4)
    assert frame_l1(np.zeros((2, 2, 3)), np.ones((2, 2, 3))) == 1.0
