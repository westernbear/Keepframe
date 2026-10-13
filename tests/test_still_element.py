import numpy as np

from keepframe.analyze.pipeline import _held_in_pass1
from keepframe.analyze.plate import PlateModel


def test_a_still_element_pass1_kept_is_found_inside_the_filled_holes():
    """A footer bar that holds still through the whole shot is in pass 1's median; the plate filled it as a hole, so
    the regions cut against pass 1 only outlined it and must be cut again (hard_cut's footer, B1b)."""
    flat = np.full((60, 100, 3), (245, 247, 251), np.uint8)
    pass1 = flat.copy()
    pass1[40:52, 10:90] = (0, 179, 143)   # the bar, in the median over time
    model = PlateModel("color", flat, (245, 247, 251), None, stats={"holes": [{"bbox": [8, 38, 92, 54]}]})
    assert _held_in_pass1(model, pass1) == 12 * 80
    assert _held_in_pass1(PlateModel("color", flat, (245, 247, 251), None, stats={"holes": []}), pass1) == 0   # not a hole: kept
    assert _held_in_pass1(PlateModel("image", pass1, (200, 200, 200), None, stats={"holes": [{"bbox": [8, 38, 92, 54]}]}), pass1) == 0
    assert _held_in_pass1(model, None) == 0
