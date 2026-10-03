import cv2
import numpy as np
import pytest
from keepframe.analyze.regions import Region
from keepframe.analyze.tracking import _coverage, _mask_coverage, _predicted_bbox, match_cost, track_regions


def _r(f, x0, w, color):
    m = np.ones((20, w), bool)
    return Region(frame=f, label=0, color=color, bbox=(x0, 10, x0 + w, 30), area=int(m.sum()), centroid=(x0 + w / 2, 20.0), mask=m)


def test_merged_blob_does_not_spawn_a_third_object():
    red, blue = (220.0, 30.0, 30.0), (30.0, 30.0, 220.0)
    frames = []
    for f in range(8):
        if f in (3, 4):
            frames.append([_r(f, 20 + f * 4, 60, (125.0, 30.0, 125.0))])   # both squares fused into one blob
        else:
            frames.append([_r(f, 20 + f * 4, 20, red), _r(f, 60 + f * 4, 20, blue)])
    tracks = track_regions(frames, cost_thr=2.0, max_gap=3)
    assert len(tracks) == 2
    assert all(3 not in t.regions and 4 not in t.regions for t in tracks)
    assert all(set(t.regions) == {0, 1, 2, 5, 6, 7} for t in tracks)


@pytest.mark.parametrize("blob_frames", [(3, 4, 5), (3, 4, 5, 6)])
def test_long_blob_keeps_tracks_alive_with_default_max_gap(blob_frames):
    red, blue = (220.0, 30.0, 30.0), (30.0, 30.0, 220.0)
    frames = []
    for f in range(blob_frames[-1] + 4):
        if f in blob_frames:
            frames.append([_r(f, 20 + f * 4, 60, (125.0, 30.0, 125.0))])
        else:
            frames.append([_r(f, 20 + f * 4, 20, red), _r(f, 60 + f * 4, 20, blue)])

    tracks = track_regions(frames, cost_thr=2.0)
    assert len(tracks) == 2
    observed_frames = set(range(len(frames))) - set(blob_frames)
    for track, index in zip(tracks, (0, 1)):
        assert set(track.regions) == observed_frames
        assert all(track.regions[f] is frames[f][index] for f in observed_frames)


def test_blob_column_cannot_steal_a_valid_assignment():
    red, blue = (220.0, 30.0, 30.0), (30.0, 30.0, 220.0)
    previous = [_r(0, 20, 20, red), _r(0, 60, 20, blue)]
    blob = _r(1, 20, 60, red)
    candidates = [_r(1, 20, 20, (160.0, 90.0, 30.0)), _r(1, 60, 20, blue)]
    assert (match_cost(previous[0], previous[0].centroid, blob, 80.0)
            < match_cost(previous[0], previous[0].centroid, candidates[0], 80.0) < 2.0)

    tracks = track_regions([previous, [blob, *candidates]], cost_thr=2.0)
    assert len(tracks) == 2
    for track, index in zip(tracks, (0, 1)):
        assert set(track.regions) == {0, 1}
        assert track.regions[1] is candidates[index]


def test_panel_with_holes_does_not_suppress_a_region():
    red, blue = (220.0, 30.0, 30.0), (30.0, 30.0, 220.0)
    frames = [[_r(f, 20 + f * 4, 20, red), _r(f, 60 + f * 4, 20, blue)] for f in range(3)]
    tracks = track_regions(frames[:2], cost_thr=2.0)
    mask = np.ones((40, 80), bool)
    mask[10:30, 8:28] = False
    mask[10:30, 48:68] = False
    ys, xs = np.nonzero(mask)
    panel = Region(frame=2, label=0, color=(30.0, 220.0, 30.0), bbox=(20, 0, 100, 40),
                   area=int(mask.sum()), centroid=(float(xs.mean() + 20), float(ys.mean())), mask=mask)
    assert panel.area >= 1.2 * max(t.regions[t.last].area for t in tracks)
    for track in tracks:
        assert _coverage(panel.bbox, _predicted_bbox(track, 2)) >= 0.5
        assert _mask_coverage(panel, track, 2) == 0.0

    frames[2].append(panel)
    tracks = track_regions(frames, cost_thr=2.0)
    assert len(tracks) == 3
    for track, index in zip(tracks[:2], (0, 1)):
        assert set(track.regions) == {0, 1, 2}
        assert track.regions[2] is frames[2][index]
    assert tracks[2].regions == {2: panel}


def test_separate_objects_passing_close_have_continuous_tracks():
    red, blue = (220.0, 30.0, 30.0), (30.0, 30.0, 220.0)
    ys, xs = np.ogrid[:20, :20]
    mask = (xs - 9.5) ** 2 + (ys - 9.5) ** 2 <= 100
    frames = []
    for f in range(8):
        regions = []
        foreground = np.zeros((64, 144), np.uint8)
        for x0, y0, color in ((20 + f * 12, 10, red), (104 - f * 12, 28, blue)):
            regions.append(Region(frame=f, label=0, color=color, bbox=(x0, y0, x0 + 20, y0 + 20),
                                  area=int(mask.sum()), centroid=(x0 + 9.5, y0 + 9.5), mask=mask.copy()))
            foreground[y0:y0 + 20, x0:x0 + 20] |= mask.astype(np.uint8)
        assert cv2.connectedComponents(foreground)[0] == 3
        frames.append(regions)

    for f in (3, 4):
        a, b = (r.bbox for r in frames[f])
        assert min(a[2], b[2]) - max(a[0], b[0]) == 8
        assert min(a[3], b[3]) - max(a[1], b[1]) == 2
    tracks = track_regions(frames, cost_thr=2.0)
    assert len(tracks) == 2
    for track, index in zip(tracks, (0, 1)):
        assert set(track.regions) == set(range(8))
        assert all(track.regions[f] is frames[f][index] for f in range(8))
