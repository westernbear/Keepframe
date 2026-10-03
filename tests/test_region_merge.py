import numpy as np
from keepframe.analyze.regions import build_palette, extract_regions, merge_adjacent_regions


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
