import numpy as np
from keepframe.ir.schema import Scene, Element, Canonical, Background, Keyframe, Track
from keepframe.ir.tracks import PRESET_EASES
from keepframe.verify.matrix import animation_matrix, bbox_matrix, extract_motions, COLS


def scene():
    e1 = Element(id="e1", kind="sprite", canonical=Canonical(width=20, height=20), visible=(0, 59),
                 tracks={"x": Track(keys=[Keyframe(t=0, v=0, ease=PRESET_EASES["out_quad"]), Keyframe(t=20, v=100)]),
                         "rot": Track(keys=[Keyframe(t=30, v=0), Keyframe(t=50, v=90)])})
    e2 = Element(id="e2", kind="sprite", canonical=Canonical(width=10, height=10), visible=(10, 59),
                 tracks={"opacity": Track(keys=[Keyframe(t=10, v=0), Keyframe(t=20, v=1)]),
                         "sx": Track(keys=[Keyframe(t=25, v=1), Keyframe(t=45, v=2)]),
                         "sy": Track(keys=[Keyframe(t=25, v=1), Keyframe(t=45, v=2)])})
    return Scene(id="s", size=(200, 200), fps=30, frames=60, background=Background(), elements=[e1, e2])


def test_matrix_shape_and_nan_outside_visible():
    m = animation_matrix(scene())
    assert m["e1"].shape == (60, len(COLS)) and m["e2"].shape == (60, 6)
    assert np.isnan(m["e2"][5]).all() and not np.isnan(m["e2"][10]).any()
    assert m["e1"][20, 0] == 100.0


def test_bbox_matrix():
    b = bbox_matrix(scene())
    assert np.allclose(b["e1"][0], [-10, -10, 10, 10])


def test_motions_extracted_in_order_with_types():
    ms = extract_motions(scene())
    by = {m.id: m for m in ms}
    assert [m.id for m in ms if m.element == "e1"] == ["m_e1_1", "m_e1_2"]
    t = by["m_e1_1"]
    assert t.type == "translation" and t.start == 0 and 18 <= t.end <= 20 and t.dir == (1.0, 0.0) and abs(t.mag - 100) < 1
    r = by["m_e1_2"]
    assert r.type == "rotation" and r.start == 30 and abs(r.mag - 90) < 1 and r.dir is None
    o, s = by["m_e2_1"], by["m_e2_2"]
    assert o.type == "opacity" and s.type == "scale" and abs(s.mag - 2.0) < 0.05
