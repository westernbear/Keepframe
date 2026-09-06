# tests/test_regions.py
import numpy as np
from refstudio.analyze.background import foreground_mask
from refstudio.analyze.regions import build_palette, extract_regions

def frame_with_rects():
    f = np.full((100, 200, 3), (0x10, 0x14, 0x18), np.uint8)
    f[10:40, 20:60] = (239, 71, 111)     # red rect
    f[50:90, 120:180] = (6, 214, 160)    # green rect
    f[60:70, 20:30] = (239, 71, 111)     # small red square (same colour, separate component)
    return f

def test_regions_split_by_colour_and_component():
    f = frame_with_rects(); bg = (0x10, 0x14, 0x18)
    fg = foreground_mask(f, bg)
    pal = build_palette(f[None], fg[None])
    regs = extract_regions(0, f, fg, pal, min_area=30)
    assert len(regs) == 3
    big_red = max((r for r in regs if r.color[0] > 200), key=lambda r: r.area)
    assert big_red.bbox == (20, 10, 60, 40) and big_red.area == 1200 and big_red.mask.shape == (30, 40)
    assert abs(big_red.centroid[0] - 39.5) < 1e-6 and abs(big_red.centroid[1] - 24.5) < 1e-6

def test_exclude_mask_and_override():
    f = frame_with_rects(); bg = (0x10, 0x14, 0x18)
    fg = foreground_mask(f, bg); pal = build_palette(f[None], fg[None])
    ex = np.zeros_like(fg); ex[50:90, 120:180] = True
    regs = extract_regions(0, f, fg, pal, exclude_mask=ex)
    assert all(r.color[1] < 200 for r in regs)                      # green removed
    ov = np.zeros_like(fg); ov[10:40, 20:60] = True; ov[60:70, 20:30] = True
    regs = extract_regions(0, f, fg, pal, overrides=[(ov, 7)])
    assert any(r.label == 7 and r.area == 1300 for r in regs)      # forced single region
