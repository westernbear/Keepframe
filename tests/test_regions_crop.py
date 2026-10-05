"""Exact parity with the original full-frame component extraction."""
import cv2
import numpy as np
import pytest

from keepframe.analyze import regions
from keepframe.analyze.background import rgb_to_lab
from keepframe.analyze.regions import Region, extract_regions, label_frame


# Frozen reference copied from the implementation before the crop optimization.
def _reference_region(frame_idx, frame, comp_mask, label):
    ys, xs = np.nonzero(comp_mask)
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1
    color = tuple(float(v) for v in frame[comp_mask].mean(0))
    return Region(frame=frame_idx, label=label, color=color, bbox=(x0, y0, x1, y1), area=int(comp_mask.sum()),
                  centroid=(float(xs.mean()), float(ys.mean())), mask=comp_mask[y0:y1, x0:x1].copy())


def _reference_extract(frame_idx, frame, fg_mask, palette_lab, min_area=30,
                       exclude_mask=None, overrides=None):
    fg = fg_mask.copy()
    if exclude_mask is not None:
        fg &= ~exclude_mask
    out = []
    for mask, label in overrides or []:
        m = mask & fg
        if m.any():
            out.append(_reference_region(frame_idx, frame, m, label)); fg &= ~mask
    labels = label_frame(frame, fg, palette_lab)
    for lab in np.unique(labels[labels >= 0]):
        n, comp, stats, _ = cv2.connectedComponentsWithStats((labels == lab).astype(np.uint8), connectivity=8)
        for c in range(1, n):
            if stats[c, cv2.CC_STAT_AREA] >= min_area:
                out.append(_reference_region(frame_idx, frame, comp == c, int(lab)))
    return out


def _assert_identical(actual, expected):
    assert len(actual) == len(expected)
    for got, want in zip(actual, expected):
        for field in ("frame", "label", "color", "bbox", "area", "centroid"):
            assert getattr(got, field) == getattr(want, field), field
        np.testing.assert_array_equal(got.mask, want.mask)
        assert got.mask.dtype == want.mask.dtype
        assert got.mask.flags.owndata


@pytest.mark.parametrize("seed", range(5))
@pytest.mark.parametrize("min_area", [1, 7, 30])
def test_random_palette_frames_with_overrides_and_exclusion_are_identical(seed, min_area):
    rng = np.random.default_rng(seed)
    colors = rng.integers(0, 256, (5, 3), dtype=np.uint8)
    frame = colors[rng.integers(0, len(colors), (67, 103))]
    fg = rng.random(frame.shape[:2]) > 0.12
    exclude = rng.random(fg.shape) < 0.08
    first = np.zeros_like(fg); first[3:19, 7:28] = True; first[40:43, 85:94] = True
    second = np.zeros_like(fg); second[13:29, 19:36] = True
    overrides = [(first, 1001), (second, 1002)]
    palette = rgb_to_lab(colors.reshape(-1, 1, 3)).reshape(-1, 3)
    expected = _reference_extract(17, frame, fg, palette, min_area, exclude, overrides)
    actual = extract_regions(17, frame, fg, palette, min_area, exclude, overrides)
    assert [r.label for r in actual[:2]] == [1001, 1002]
    _assert_identical(actual, expected)


def test_crop_region_keeps_exact_global_centroid_and_tight_mask():
    frame = np.random.default_rng(71).integers(0, 256, (80, 120, 3), dtype=np.uint8)
    sub = np.zeros((7, 9), bool)
    sub[2, 3:6] = True
    sub[3, 4:6] = True
    sub[4, 5] = True
    full = np.zeros(frame.shape[:2], bool)
    full[39:46, 79:88] = sub
    expected = _reference_region(4, frame, full, 2)
    actual = regions._region_crop(4, frame, sub, 79, 39, 2)
    _assert_identical([actual], [expected])
    sub[:] = False
    assert actual.mask.any()


def test_palette_components_only_scan_their_bounding_boxes(monkeypatch):
    frame = np.zeros((180, 300, 3), np.uint8)
    frame[9:14, 22:29] = (230, 80, 60)
    frame[109:118, 202:213] = (230, 80, 60)
    fg = frame[..., 0] > 0
    palette = rgb_to_lab(np.array([[[230, 80, 60]]], np.uint8)).reshape(-1, 3)
    scanned = []
    original = np.nonzero

    def nonzero(mask):
        scanned.append(mask.shape)
        return original(mask)

    monkeypatch.setattr(regions.np, "nonzero", nonzero)
    assert len(extract_regions(0, frame, fg, palette)) == 2
    assert scanned == [(5, 7), (9, 11)]
