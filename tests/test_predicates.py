import pytest
from keepframe.ir.schema import Scene, Element, Canonical, Background, Keyframe, Track
from keepframe.verify.predicates import parse_pred, build_context, eval_pred


def scene():
    e1 = Element(id="e1", kind="sprite", canonical=Canonical(width=20, height=20), visible=(0, 59),
                 tracks={"x": Track(keys=[Keyframe(t=0, v=0), Keyframe(t=20, v=100)]), "y": Track(keys=[Keyframe(t=0, v=50)])})
    e2 = Element(id="e2", kind="sprite", canonical=Canonical(width=20, height=20), visible=(0, 59),
                 tracks={"x": Track(keys=[Keyframe(t=0, v=150)]), "y": Track(keys=[Keyframe(t=0, v=50)]),
                         "rot": Track(keys=[Keyframe(t=30, v=0), Keyframe(t=40, v=45)])})
    return Scene(id="s", size=(200, 100), fps=30, frames=60, background=Background(), elements=[e1, e2])


def test_parse():
    assert parse_pred("dir(m_e1_1,[1,0])") == ("dir", ["m_e1_1", [1.0, 0.0]], None)
    assert parse_pred("type(m_e1_1,'translation')") == ("type", ["m_e1_1", "translation"], None)
    assert parse_pred("left(e1,e2)@10") == ("left", ["e1", "e2"], 10)


@pytest.mark.parametrize("pred,expected", [
    ("type(m_e1_1,'translation')", True), ("type(m_e1_1,'rotation')", False),
    ("dir(m_e1_1,[1,0])", True), ("dir(m_e1_1,[-1,0])", False),
    ("mag(m_e1_1,100)", True), ("mag(m_e1_1,60)", False),
    ("dur(m_e1_1,20)", True), ("dur(m_e1_1,10)", False),
    ("before(m_e1_1,m_e2_1)", True), ("after(m_e1_1,m_e2_1)", False), ("while(m_e1_1,m_e2_1)", False),
    ("left(e1,e2)", True), ("right(e1,e2)", False), ("intersect(e1,e2)", False), ("left(e1,e2)@0", True),
    ("type(m_e9_1,'translation')", False),
])
def test_eval(pred, expected):
    ctx = build_context(scene())
    assert eval_pred(pred, ctx) is expected


def test_unknown_predicate_raises():
    with pytest.raises(ValueError):
        eval_pred("dance(e1)", build_context(scene()))
