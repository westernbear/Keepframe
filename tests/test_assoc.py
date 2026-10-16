import json

import numpy as np

from keepframe.analyze.assoc import N_FEATURES, Matcher, descriptor, load_matcher, pair_features
from keepframe.analyze.regions import Region
from keepframe.analyze.tracking import _merge_adjacent_tracks, track_regions


def _reg(f, mask, label=1, color=(200.0, 80.0, 40.0)):
    ys, xs = np.nonzero(mask)
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1
    return Region(frame=f, label=label, color=color, bbox=(x0, y0, x1, y1), area=int(mask.sum()),
                  centroid=(float(xs.mean()), float(ys.mean())), mask=mask[y0:y1, x0:x1].copy())


def _frames(n=3):
    rng = np.random.default_rng(0)
    return rng.integers(0, 255, (n, 60, 100, 3), np.uint8)


def test_pair_features_are_finite_and_a_region_matches_itself():
    frames, m = _frames(), np.zeros((60, 100), bool)
    m[10:30, 20:50] = True
    a, b = _reg(0, m), _reg(1, m)
    fa = pair_features(a, descriptor(a, frames[0]), a, descriptor(a, frames[0]), 1)
    fb = pair_features(a, descriptor(a, frames[0]), b, descriptor(b, frames[1]), 1)
    assert fa.shape == (N_FEATURES,) and np.isfinite(fa).all() and np.isfinite(fb).all()
    assert fa[2] == 1.0 and fa[5] == 1.0 and abs(fa[13] - 1.0) < 1e-5   # bbox IoU, aligned IoU, grey correlation
    assert np.array_equal(fa, pair_features(a, descriptor(a, frames[0]), a, descriptor(a, frames[0]), 1))   # deterministic


def _weights(tmp_path, **extra):
    w = {"mean": np.zeros(N_FEATURES), "std": np.ones(N_FEATURES), "W1": np.eye(N_FEATURES, 2), "b1": np.zeros(2),
         "W2": np.eye(2), "b2": np.zeros(2), "W3": np.array([[1.0], [-1.0]]), "b3": np.zeros(1),
         "meta": np.array(json.dumps({"note": "test"})), **extra}
    path = tmp_path / "assoc.npz"
    np.savez(path, **w)
    return path


def test_numpy_mlp_and_loading(tmp_path):
    m = load_matcher(str(_weights(tmp_path)))
    x = np.zeros(N_FEATURES, np.float32)
    x[0], x[1] = 2.0, 0.5           # relu(x W1) = (2, 0.5) -> W3: 2 - 0.5 = 1.5
    assert abs(m.prob(x)[0] - 1 / (1 + np.exp(-1.5))) < 1e-6 and m.meta == {"note": "test"}
    assert load_matcher(str(tmp_path / "missing.npz")) is None
    (tmp_path / "bad").mkdir()
    assert load_matcher(str(_weights(tmp_path / "bad", mean=np.zeros(5)))) is None   # weights that do not fit


class _ByLabel:
    """A stand-in matcher: same content exactly when the two regions carry the same label (feature-free)."""

    def __init__(self):
        self.pairs = []

    def prob(self, x):
        return np.array([1.0 if a == b else 0.0 for a, b in self.pairs.pop(0)])


def test_tracking_follows_the_matcher(monkeypatch):
    import keepframe.analyze.tracking as tr
    frames = _frames(6)
    box, other = np.zeros((60, 100), bool), np.zeros((60, 100), bool)
    box[10:30, 20:40] = True
    other[12:32, 24:44] = True        # nearly where box was: the rules would continue box's track with it
    rbf = [[_reg(f, box, label=1)] for f in range(3)] + [[_reg(f, other, label=2)] for f in range(3, 6)]
    fake = _ByLabel()

    def probs(matcher, desc, pairs):
        fake.pairs.append([(a.label, b.label) for a, b, _ in pairs])
        return fake.prob(None)

    monkeypatch.setattr(tr, "_probs", probs)
    learned = track_regions(rbf, cost_thr=2.0, frames=frames, matcher=fake)
    assert sorted(sorted(t.regions) for t in learned) == [[0, 1, 2], [3, 4, 5]]
    assert [sorted(t.regions) for t in track_regions(rbf, cost_thr=2.0)] == [[0, 1, 2, 3, 4, 5]]   # the rules join them
    assert len(_merge_adjacent_tracks(learned, frames=frames, matcher=fake)) == 2
