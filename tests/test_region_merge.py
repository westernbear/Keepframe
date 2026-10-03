import cv2
import numpy as np
import pytest
from keepframe.analyze import regions as region_module
from keepframe.analyze.regions import (
    _K3, _boxes_touch, _edge_jump, _region, build_palette, extract_regions,
    merge_adjacent_regions, rgb_to_lab,
)


def _frame():
    f = np.zeros((80, 160, 3), np.uint8)
    for x in range(10, 70):                                  # gradient bar: many palette bands
        f[10:40, x] = (200 - (x - 10) * 2, 60, 60 + (x - 10) * 2)
    f[50:70, 10:30] = (250, 250, 250)                        # white square
    f[50:70, 30:50] = (20, 40, 240)                          # touching blue square (far colour)
    f[50:70, 100:120] = (250, 250, 250)                      # separate white square
    return f


def test_gradient_bands_merge_but_distinct_shapes_do_not():
    f = _frame(); fg = f.sum(2) > 0
    regions = extract_regions(0, f, fg, build_palette(f[None], fg[None], k=8), min_area=10)
    merged = merge_adjacent_regions(0, f, regions)
    bar = [r for r in merged if r.bbox[1] < 45]
    assert len(bar) == 1 and bar[0].area >= 0.95 * 60 * 30
    assert len([r for r in merged if r.bbox[1] >= 45]) == 3


def _edge_jump_union_window(a, b, lab):
    # Snapshot of the pre-optimization edge calculation for exact equivalence.
    H, W = lab.shape[:2]
    x0, y0 = max(0, min(a.bbox[0], b.bbox[0]) - 1), max(0, min(a.bbox[1], b.bbox[1]) - 1)
    x1, y1 = min(W, max(a.bbox[2], b.bbox[2]) + 1), min(H, max(a.bbox[3], b.bbox[3]) + 1)
    def place(r):
        m = np.zeros((y1 - y0, x1 - x0), np.uint8)
        m[r.bbox[1] - y0:r.bbox[3] - y0, r.bbox[0] - x0:r.bbox[2] - x0] = r.mask
        return m
    ma, mb = place(a), place(b)
    edge_a = (ma & cv2.dilate(mb, _K3)).astype(bool)
    edge_b = (mb & cv2.dilate(ma, _K3)).astype(bool)
    if not edge_a.any() or not edge_b.any():
        return None
    win = lab[y0:y1, x0:x1]
    return float(np.linalg.norm(win[edge_a].mean(0) - win[edge_b].mean(0)))


def _pair(frame, box_a, box_b, labels=(0, 1)):
    masks = [np.zeros(frame.shape[:2], bool) for _ in labels]
    for mask, (x0, y0, x1, y1) in zip(masks, (box_a, box_b)):
        mask[y0:y1, x0:x1] = True
    return [_region(0, frame, mask, label) for mask, label in zip(masks, labels)]


@pytest.mark.parametrize("case", ["horizontal", "vertical", "corner", "large_l", "disjoint"])
def test_edge_jump_matches_union_window_in_shared_edge_crop(case, monkeypatch):
    frame = np.random.default_rng(16).integers(0, 256, (120, 160, 3), dtype=np.uint8)
    if case == "horizontal":
        a, b = _pair(frame, (10, 10, 50, 60), (50, 10, 90, 60))
    elif case == "vertical":
        a, b = _pair(frame, (0, 0, 40, 40), (0, 40, 40, 80))
    elif case == "corner":
        a, b = _pair(frame, (120, 80, 140, 100), (140, 100, 160, 120))
    elif case == "large_l":
        mask = np.zeros(frame.shape[:2], bool)
        mask[5:90, 5:15] = True
        mask[80:90, 5:110] = True
        a = _region(0, frame, mask, 0)
        _, b = _pair(frame, (5, 5, 15, 15), (70, 60, 90, 80))
    else:
        a, b = _pair(frame, (10, 10, 30, 30), (80, 60, 100, 80))
    lab = rgb_to_lab(frame)
    expected = _edge_jump_union_window(a, b, lab)
    shapes = []
    dilate = cv2.dilate
    def record_dilate(mask, kernel):
        shapes.append(mask.shape)
        return dilate(mask, kernel)
    monkeypatch.setattr(region_module.cv2, "dilate", record_dilate)
    assert _edge_jump(a, b, lab) == expected
    if case == "disjoint":
        assert expected is None
        assert shapes == []
    else:
        assert expected is not None
        H, W = frame.shape[:2]
        x0, y0 = max(0, max(a.bbox[0], b.bbox[0]) - 1), max(0, max(a.bbox[1], b.bbox[1]) - 1)
        x1, y1 = min(W, min(a.bbox[2], b.bbox[2]) + 1), min(H, min(a.bbox[3], b.bbox[3]) + 1)
        assert shapes == [(y1 - y0, x1 - x0)] * 2


def test_gradient_merge_matches_full_frame_rebuild_using_union_crop(monkeypatch):
    frame = _frame(); fg = frame.sum(2) > 0
    regions = extract_regions(7, frame, fg, build_palette(frame[None], fg[None], k=8), min_area=10)
    bands = [r for r in regions if r.bbox[1] < 45]
    full = np.zeros(frame.shape[:2], bool)
    for r in bands:
        full[r.bbox[1]:r.bbox[3], r.bbox[0]:r.bbox[2]] |= r.mask
    expected_bar = _region(7, frame, full, max(bands, key=lambda r: r.area).label)
    expected = [expected_bar] + [r for r in regions if r.bbox[1] >= 45]
    shapes = []
    zeros = np.zeros
    def record_zeros(shape, dtype=float, *args, **kwargs):
        if np.dtype(dtype) == np.dtype(bool):
            shapes.append(shape)
        return zeros(shape, dtype, *args, **kwargs)
    monkeypatch.setattr(region_module.np, "zeros", record_zeros)
    merged = merge_adjacent_regions(7, frame, regions)
    assert len(merged) == len(expected) == 4
    for actual in merged:
        old = next(r for r in expected if r.bbox == actual.bbox)
        assert (actual.frame, actual.label, actual.bbox, actual.area, actual.centroid, actual.color) == (
            old.frame, old.label, old.bbox, old.area, old.centroid, old.color)
        np.testing.assert_array_equal(actual.mask, old.mask)
        if old is not expected_bar:
            assert actual is old
    assert shapes == [(30, 60)]


@pytest.mark.parametrize("label", [1000, 1007])
@pytest.mark.parametrize("manual_first", [False, True])
def test_manual_override_regions_never_merge(label, manual_first):
    frame = np.full((30, 40, 3), 100, np.uint8)
    labels = (label, 0) if manual_first else (0, label)
    regions = _pair(frame, (5, 5, 20, 25), (20, 5, 35, 25), labels)
    assert _edge_jump(*regions, rgb_to_lab(frame)) == 0.0
    merged = merge_adjacent_regions(0, frame, regions)
    assert len(merged) == 2
    assert all(actual is old for actual, old in zip(merged, regions))


@pytest.mark.parametrize("count", [0, 1])
def test_fewer_than_two_regions_pass_through_without_lab(count, monkeypatch):
    frame = np.full((30, 40, 3), 100, np.uint8)
    regions = _pair(frame, (5, 5, 20, 25), (20, 5, 35, 25))[:count]
    def unexpected_lab(frame):
        pytest.fail("passthrough should not convert the frame to LAB")
    monkeypatch.setattr(region_module, "rgb_to_lab", unexpected_lab)
    assert merge_adjacent_regions(0, frame, regions) is regions


def test_touching_bboxes_without_mask_contact_do_not_merge():
    frame = np.full((40, 60, 3), 100, np.uint8)
    ma, mb = [np.zeros(frame.shape[:2], bool) for _ in range(2)]
    ma[10:30, 10:12] = True; ma[10:12, 10:30] = True
    mb[10:30, 48:50] = True; mb[28:30, 30:50] = True
    regions = [_region(0, frame, ma, 0), _region(0, frame, mb, 1)]
    assert _boxes_touch(*regions)
    assert _edge_jump(*regions, rgb_to_lab(frame)) is None
    merged = merge_adjacent_regions(0, frame, regions)
    assert len(merged) == 2
    assert all(actual is old for actual, old in zip(merged, regions))


@pytest.mark.parametrize("jump, count", [(3.0, 1), (float(np.nextafter(np.float32(3), np.float32(np.inf))), 2)])
def test_jump_threshold_is_inclusive(jump, count, monkeypatch):
    frame = np.full((30, 40, 3), 100, np.uint8)
    regions = _pair(frame, (5, 5, 20, 25), (20, 5, 35, 25))
    lab = np.zeros(frame.shape, np.float32)
    lab[:, 20:, 0] = jump
    monkeypatch.setattr(region_module, "rgb_to_lab", lambda frame: lab)
    assert _edge_jump(*regions, lab) == jump
    merged = merge_adjacent_regions(0, frame, regions, max_jump=3.0)
    assert len(merged) == count
    if count == 2:
        assert all(actual is old for actual, old in zip(merged, regions))
