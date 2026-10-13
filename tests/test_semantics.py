import numpy as np
from keepframe.ir.schema import Element, Canonical, Keyframe, Track
from keepframe.analyze.semantics import assign_roles, group_by_motion, NullCaptioner


def el(i, w, h, kind="sprite", vis=(0, 59)):
    return Element(id=f"e{i}", kind=kind, canonical=Canonical(width=w, height=h, text="T" if kind == "text" else None), visible=vis)


def test_roles():
    els = [el(1, 50, 50), el(2, 200, 100), el(3, 80, 20, kind="text")]
    assign_roles(els)
    assert [e.role for e in els] == ["secondary", "primary", "text"]


def test_group_by_motion():
    n = 30
    base = np.zeros((n, 8)); base[:, 0] = np.linspace(0, 100, n); base[:, 1] = np.linspace(0, 50, n); base[:, 2:4] = 1; base[:, 7] = 1
    other = base.copy(); other[:, 0] = np.linspace(100, 0, n)
    raws = {"e1": base, "e2": base + np.array([40, 0, 0, 0, 0, 0, 0, 0]), "e3": other}
    els = [el(1, 10, 10, vis=(0, n - 1)), el(2, 10, 10, vis=(0, n - 1)), el(3, 10, 10, vis=(0, n - 1))]
    g = group_by_motion(els, raws)
    assert len(g) == 1 and sorted(g[0].members) == ["e1", "e2"] and g[0].reason == "moves together"


def test_null_captioner():
    assert NullCaptioner().caption(np.zeros((2, 2, 4), np.uint8), None) == ""


def test_successions_group_what_takes_over_in_place():
    """A counter's values: e2 starts the frame after e1 ends in the same box, e3 two frames later; e4 starts there
    but elsewhere, e5 in the same box but 5 frames late. Tracks are untouched."""
    from keepframe.analyze.semantics import group_successions
    from keepframe.ir.schema import Canonical, Element, Keyframe, Track

    def el(i, vis, x):
        return Element(id=i, kind="text", canonical=Canonical(width=40, height=20), visible=vis,
                       tracks={"x": Track(keys=[Keyframe(t=0, v=x)]), "y": Track(keys=[Keyframe(t=0, v=50)])})

    els = [el("e1", (0, 9), 100), el("e2", (10, 19), 102), el("e3", (21, 30), 100), el("e4", (10, 19), 300), el("e5", (36, 40), 100)]
    groups = group_successions(els)
    assert [(g.members, g.reason) for g in groups] == [(["e1", "e2", "e3"], "same place, one after another")]
