import math

import cv2
import numpy as np
import pytest

from keepframe.analyze.regions import Region
from keepframe.analyze.sprites import _ecc_constants, _refine_ecc_xy, refine_ecc


def _affine_sprite(side=640, scale=1.1):
    canon = np.zeros((side, side, 4), np.uint8)
    cv2.rectangle(canon, (side // 10, side // 5), (side * 9 // 10, side * 4 // 5), (160, 90, 210, 255), -1)
    cv2.circle(canon, (side // 4, side // 3), side // 10, (240, 200, 60, 255), -1)
    cv2.rectangle(canon, (side // 2, side // 2), (side * 3 // 4, side * 2 // 3), (40, 230, 130, 255), -1)
    cv2.line(canon, (side // 6, side * 2 // 3), (side * 2 // 3, side // 4), (255, 255, 255, 255), max(2, side // 50))
    frame_side = side + 320
    center = frame_side / 2
    # Independent OpenCV renderer: 7.3 px translation and clockwise 4 degrees.
    warp = cv2.getRotationMatrix2D((side / 2, side / 2), -4, scale)
    warp[:, 2] += [center + 7.3 - side / 2, center - side / 2]
    rgba = cv2.warpAffine(canon, warp, (frame_side, frame_side))
    full_mask = rgba[..., 3] > 127
    ys, xs = np.nonzero(full_mask)
    x0, y0, x1, y1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
    region = Region(0, 0, (160.0, 90.0, 210.0), (x0, y0, x1, y1), int(full_mask.sum()),
                    (float(xs.mean()), float(ys.mean())), full_mask[y0:y1, x0:x1].copy())
    init = dict(x=center, y=center, sx=scale, sy=scale, rot=1.0, skx=0.0, sky=0.0, opacity=0.7)
    return rgba[..., :3], region, canon, init, (center + 7.3, center)


@pytest.mark.parametrize("xy_only", [False, True])
@pytest.mark.parametrize("side,scale", [(640, 1.1), (641, 1.1), (320, 1.7)])
def test_sprite_ecc_uses_capped_inputs_and_recovers_known_affine(monkeypatch, xy_only, side, scale):
    frame, region, canon, init, expected_xy = _affine_sprite(side, scale)
    original = cv2.findTransformECC
    shapes = []

    def capped_ecc(image, template, *args):
        shapes.append((image.shape, template.shape))
        assert max(*image.shape, *template.shape) <= 384
        return original(image, template, *args)

    monkeypatch.setattr(cv2, "findTransformECC", capped_ecc)
    if xy_only:
        L, tpl = _ecc_constants(canon, (0.5, 0.5))
        props = _refine_ecc_xy(frame, region, L, tpl, init)
    else:
        props = refine_ecc(frame, region, canon, (0.5, 0.5), init)
    assert shapes
    assert math.hypot(props["x"] - expected_xy[0], props["y"] - expected_xy[1]) <= 0.5
    assert abs(props["rot"] - 4.0) <= 0.5
    assert props["opacity"] == init["opacity"]
    assert props["sx"] == pytest.approx(scale, abs=0.01)
    assert props["sy"] == pytest.approx(scale, abs=0.01)


def test_small_ecc_inputs_and_initial_warp_are_unchanged(monkeypatch):
    frame, region, canon, init, _ = _affine_sprite(128)
    L, template = _ecc_constants(canon, (0.5, 0.5))
    x0, y0 = max(0, region.bbox[0] - 8), max(0, region.bbox[1] - 8)
    x1, y1 = min(frame.shape[1], region.bbox[2] + 8), min(frame.shape[0], region.bbox[3] + 8)
    expected_image = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0
    warp = cv2.getRotationMatrix2D((0, 0), -init["rot"], init["sx"])
    warp[:, 2] = [init["x"] - x0, init["y"] - y0]
    expected_warp = np.linalg.inv(np.vstack([warp, [0, 0, 1]]) @ L)[:2].astype(np.float32)

    def unchanged_ecc(image, tpl, initial, *args):
        np.testing.assert_array_equal(image, expected_image)
        np.testing.assert_array_equal(tpl, template)
        np.testing.assert_array_equal(initial, expected_warp)
        raise cv2.error("deliberately rejected")

    monkeypatch.setattr(cv2, "findTransformECC", unchanged_ecc)
    assert refine_ecc(frame, region, canon, (0.5, 0.5), init) is init


def test_resize_rejection_of_thin_sprite_keeps_original_fallback():
    frame = np.zeros((4096, 16, 3), np.uint8)
    frame[:, 7] = (255, 0, 0)
    canon = np.zeros((640, 1, 4), np.uint8)
    canon[..., 0] = canon[..., 3] = 255
    region = Region(0, 0, (255.0, 0.0, 0.0), (7, 0, 8, 4096), 4096, (7.0, 2047.5),
                    np.ones((4096, 1), bool))
    init = dict(x=7.5, y=2048.0, sx=1.0, sy=6.4, rot=0.0, skx=0.0, sky=0.0, opacity=1.0)
    assert refine_ecc(frame, region, canon, (0.5, 0.5), init) is init
