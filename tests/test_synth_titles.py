import numpy as np
import pytest
from keepframe.analyze.composite import composite_scene
from keepframe.ir.synth import make_reference_scene
from keepframe.ir.tracks import element_bbox, is_tilted, eval_props


def _others(scene):
    return [e.model_dump_json() for e in scene.elements if e.id != "title1"]


def test_title_modes_do_not_touch_other_elements(tmp_path):
    base = make_reference_scene(tmp_path / "a", 9, plate="flat")
    for mode in ("stagger", "plane"):
        s = make_reference_scene(tmp_path / mode, 9, plate="flat", title=mode)
        assert _others(s) == _others(base)
    plain = make_reference_scene(tmp_path / "p", 3, plate="flat")
    assert plain.model_dump_json() == make_reference_scene(tmp_path / "p2", 3, plate="flat", title=None).model_dump_json()
    assert plain.element("title1").canonical.glyphs is None


def test_stagger_title_reveals_glyph_by_glyph(tmp_path):
    scene = make_reference_scene(tmp_path, 9, plate="flat", title="stagger")
    t = scene.element("title1")
    g, c = t.canonical.glyphs, t.canonical
    assert len(g.x) == len("".join(c.text.split())) and g.x == sorted(g.x)
    assert 2 <= g.delays[1] - g.delays[0] <= 3 and 4 <= g.ramp <= 6
    assert [k.v for k in t.tracks["opacity"].keys] == [1.0] and c.width % 2 == 0 and c.height % 2 == 0
    v0 = t.visible[0]
    early = composite_scene(scene, tmp_path, v0 + 1)
    done = composite_scene(scene, tmp_path, v0 + int(max(g.delays) + g.ramp) + 1)
    x0, y0, x1, y1 = (int(v) for v in element_bbox(t, v0 + 1))
    mid = (x0 + x1) // 2
    ink = lambda im, a, b: float(np.abs(im[y0:y1, a:b] - composite_scene(make_reference_scene(tmp_path / "b", 9, plate="flat", n_sprites=0, logo=False, title=None), tmp_path / "b", 0)[y0:y1, a:b]).sum())
    assert ink(early, x0, mid) > 0 and ink(early, mid, x1) == 0   # only the left glyphs are in yet
    assert ink(done, mid, x1) > 0


def test_plane_title_is_tilted_gradient_faded(tmp_path):
    scene = make_reference_scene(tmp_path, 10, plate="flat", title="plane")
    t = scene.element("title1")
    assert t.kind == "text" and t.pending_asset is None and 60 <= t.canonical.font.size_px <= 72
    st = t.canonical.style
    assert len(st.fill.stops) == 2 and st.fade.angle == 90 and [s.alpha for s in st.fade.stops] == [1.0, 0.3]
    v0, v1 = eval_props(t, t.visible[0]), eval_props(t, scene.frames - 1)
    assert v0["ry"] == -35 and v1["ry"] == -18 and abs(v0["rot"]) <= 3 and is_tilted(v1, "text")


def test_title_whole_gates_aggregate():
    from keepframe.qa.edits import SYNTHETIC_GATES, clip_gates, synthetic_gates
    assert SYNTHETIC_GATES["title_whole"] == ("integrity.whole", "rate", 0.9)
    seed = lambda *w: {"integrity": [{"whole": x} for x in w]}
    g = synthetic_gates([seed(True, True), seed(True, False)])["title_whole"]
    assert g["value"] == 0.75 and g["samples"] == 4 and g["passed"] is False
    clip = lambda *w: {"integrity": [{"whole": x, "role": "title"} for x in w] + [{"whole": True, "role": "kinetic"}],
                       "colour": [], "render_l1": 0.0, "seconds": 1.0, "vlm_calls": 0, "baseline": {}}
    g = clip_gates([clip(True, True), clip(True, False)])["title_whole"]   # kinetic entries count too: 6 of 6 whole
    assert g["samples"] == 6 and g["passed"] is True
    g = clip_gates([clip(False, False), clip(False)])["title_whole"]
    assert g["samples"] == 5 and g["passed"] is False
