import math, numpy as np, pytest
from keepframe.ir.schema import Keyframe, Track, Element, Canonical
from keepframe.ir.tracks import (bezier_y, eval_track, eval_props, eval_z, affine_matrix,
                                 decompose_affine, element_bbox, PRESET_EASES)

def test_bezier_endpoints_and_linear():
    for e in PRESET_EASES.values():
        assert bezier_y(0.0, e) == pytest.approx(0.0, abs=1e-6)
        assert bezier_y(1.0, e) == pytest.approx(1.0, abs=1e-6)
    assert bezier_y(0.3, PRESET_EASES["linear"]) == pytest.approx(0.3, abs=1e-6)
    # ease-out is ahead of linear mid-way, ease-in is behind
    assert bezier_y(0.5, PRESET_EASES["out_quad"]) > 0.5 > bezier_y(0.5, PRESET_EASES["in_quad"])

def test_eval_track_clamps_and_interpolates():
    tr = Track(keys=[Keyframe(t=10, v=0.0), Keyframe(t=20, v=100.0)])
    assert eval_track(tr, 0) == 0.0 and eval_track(tr, 99) == 100.0
    assert eval_track(tr, 15) == pytest.approx(50.0)

def test_eval_props_fills_defaults_and_z_is_stepwise():
    el = Element(id="e", kind="sprite", canonical=Canonical(width=10, height=10), visible=(0, 10),
                 tracks={"x": Track(keys=[Keyframe(t=0, v=5.0)])},
                 z=Track(keys=[Keyframe(t=0, v=1), Keyframe(t=5, v=3)]))
    p = eval_props(el, 3)
    assert p["x"] == 5.0 and p["sx"] == 1.0 and p["opacity"] == 1.0
    assert eval_z(el, 4) == 1 and eval_z(el, 5) == 3

def test_affine_roundtrip():
    p = {"x": 120.0, "y": -30.0, "sx": 1.5, "sy": 0.5, "rot": 33.0, "skx": 10.0, "sky": 0.0, "opacity": 1.0}
    M = affine_matrix(p)
    q = decompose_affine(M)
    for k in ("x", "y", "sx", "sy", "rot", "skx"):
        assert q[k] == pytest.approx(p[k], abs=1e-6), k

def test_bbox_of_scaled_rotated_box():
    el = Element(id="e", kind="sprite", canonical=Canonical(width=100, height=40), visible=(0, 0),
                 tracks={"x": Track(keys=[Keyframe(t=0, v=200)]), "y": Track(keys=[Keyframe(t=0, v=100)]),
                         "sx": Track(keys=[Keyframe(t=0, v=2.0)]), "rot": Track(keys=[Keyframe(t=0, v=90)])})
    x0, y0, x1, y1 = element_bbox(el, 0)
    # width 200 rotated 90° becomes vertical extent 200, height 40 becomes horizontal extent 40
    assert (x1 - x0) == pytest.approx(40, abs=1e-6) and (y1 - y0) == pytest.approx(200, abs=1e-6)
    assert (x0 + x1) / 2 == pytest.approx(200) and (y0 + y1) / 2 == pytest.approx(100)
