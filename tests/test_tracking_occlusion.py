import cv2
import numpy as np
from keepframe.analyze.regions import Region
from keepframe.analyze.tracking import track_regions


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
