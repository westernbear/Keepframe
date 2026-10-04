from math import comb

import pytest

from keepframe.analyze.constraints import apply_keep_preset, extract_constraints
from keepframe.ir.schema import Background, Canonical, Constraint, Element, Keyframe, Scene, Track
from keepframe.session.brief import scene_brief
from keepframe.verify.matrix import extract_motions
from keepframe.verify.predicates import build_context, eval_pred, parse_pred
from keepframe.verify.verifier import verify


def _element(eid, start=0, end=6, *, text=False, width=10, height=10, visible=(0, 39)):
    return Element(
        id=eid, kind="text" if text else "sprite", visible=visible,
        canonical=Canonical(width=width, height=height, text=eid if text else None),
        tracks={"x": Track(keys=[Keyframe(t=0, v=0), *([Keyframe(t=start, v=0)] if start else []),
                                  Keyframe(t=end, v=30), Keyframe(t=39, v=30)])},
    )


def _scene(elements):
    return Scene(id="s1", size=(640, 360), fps=30, frames=40, background=Background(), elements=elements)


def _crowded_scene():
    return _scene([_element(f"e{i}") for i in range(100)] +
                  [_element(f"title{i}", 8 * i, 8 * i + 6, text=True, width=2, height=2) for i in range(3)])


def test_sprite_fragments_keep_all_unary_facts_but_bound_binary_predicates():
    scene = _crowded_scene()
    constraints = extract_constraints(scene)
    unary = [c for c in constraints if c.pred.startswith(("type(", "dir(", "mag(", "dur("))]
    assert len(unary) == 4 * len(extract_motions(scene)) == 412
    assert len(constraints) <= len(unary) + 6 * comb(24, 2)
    from keepframe.analyze.constraints import MAX_SALIENT
    from keepframe.ir.importance import rank_elements

    assert MAX_SALIENT == 24
    salient = {e.id for e in rank_elements(scene)[:MAX_SALIENT]}
    assert {f"title{i}" for i in range(3)} <= salient
    motions = {m.id: m.element for m in extract_motions(scene)}
    for c in constraints:
        name, args, _ = parse_pred(c.pred)
        if name in {"before", "after", "while"}:
            assert {motions[a] for a in args} <= salient
        elif name not in {"type", "dir", "mag", "dur"}:
            assert set(args) <= salient
    ctx = build_context(scene)
    assert all(eval_pred(c.pred, ctx) for c in constraints)


@pytest.mark.parametrize("limit", [0, 1, 4])
def test_custom_salience_limit_does_not_drop_unary_predicates(limit):
    scene = _crowded_scene()
    constraints = extract_constraints(scene, max_salient=limit)
    unary = [c for c in constraints if c.pred.startswith(("type(", "dir(", "mag(", "dur("))]
    assert len(unary) == 412
    assert len(constraints) <= len(unary) + 6 * comb(limit, 2)


def test_before_chains_keep_all_ties_and_omit_transitive_edges_and_after():
    # Deliberately reverse element enumeration: ordering follows motion times.
    scene = _scene([_element("e4", 16, 20), _element("e3", 8, 12),
                    _element("e2", 8, 12), _element("e1", 0, 4)])
    constraints = extract_constraints(scene)
    assert {c.pred for c in constraints if c.pred.startswith("before(")} == {
        "before(m_e1_1,m_e2_1)", "before(m_e1_1,m_e3_1)",
        "before(m_e2_1,m_e4_1)", "before(m_e3_1,m_e4_1)",
    }
    assert not any(c.pred.startswith("after(") for c in constraints)
    assert "while(m_e3_1,m_e2_1)" in {c.pred for c in constraints}
    assert eval_pred("after(m_e4_1,m_e1_1)", build_context(scene))


def test_swapping_salient_motion_order_still_fails_keep_verification(tmp_path):
    scene = _scene([_element("e1", 0, 4, text=True), _element("e2", 8, 12, text=True)])
    scene.constraints = apply_keep_preset(extract_constraints(scene), "content_only")
    assert verify(scene, tmp_path).keep_failed == 0
    changed = scene.model_copy(deep=True)
    changed.elements[0].tracks, changed.elements[1].tracks = changed.elements[1].tracks, changed.elements[0].tracks
    result = verify(changed, tmp_path)
    assert result.keep_failed == 1
    assert result.keep_pass_rate < 1 and not result.passed
    assert [c["pred"] for c in result.keep_results if not c["passed"]] == ["before(m_e1_1,m_e2_1)"]


def test_spatial_relations_only_use_salient_elements_visible_on_last_frame():
    scene = _scene([_element("e1", text=True), _element("e2", text=True, visible=(0, 20)),
                    _element("e3", text=True), _element("e4", width=1, height=1)])
    constraints = extract_constraints(scene, max_salient=3)
    spatial = [parse_pred(c.pred)[1] for c in constraints if c.pred.startswith(("left(", "right(", "top(", "bottom(", "intersect("))]
    assert spatial and all(set(args) == {"e1", "e3"} for args in spatial)
    assert any("m_e2_1" in c.pred and c.pred.startswith("while(") for c in constraints)


def test_importance_preserves_brief_ranking_and_deterministic_ties():
    from keepframe.ir.importance import rank_elements

    elements = [_element("big", width=100, height=100), _element("text", text=True, width=1, height=1),
                _element("long", width=10, height=10), _element("short", width=10, height=10, visible=(0, 10)),
                _element("tie_b"), _element("tie_a"), _element("empty_text", text=True)]
    elements[-1].canonical.text = None
    scene = _scene(elements)
    original = list(scene.elements)
    assert [e.id for e in rank_elements(scene)] == ["text", "big", "empty_text", "long", "tie_a", "tie_b", "short"]
    assert scene.elements == original
    # The brief still displays its selected elements in entrance/id order.
    assert [line.split(" | ")[0] for line in scene_brief(scene).splitlines()[3:]] == sorted(e.id for e in elements)


@pytest.mark.parametrize("preset, names", [
    ("content_only", {"type", "dir", "mag", "dur", "before", "after", "while"}),
    ("motion_shape", {"type", "dir", "before", "after"}),
    ("all", {"type", "dir", "mag", "dur", "before", "after", "while", "left", "right", "top", "bottom", "intersect"}),
    ("none", set()),
])
def test_keep_preset_semantics_include_legacy_after(preset, names):
    scene = _scene([_element("e1", 0, 4), _element("e2", 8, 12)])
    constraints = [*extract_constraints(scene), Constraint(pred="after(m_e2_1,m_e1_1)")]
    locked = apply_keep_preset(constraints, preset)
    assert all(c.keep == (parse_pred(c.pred)[0] in names) for c in locked)
