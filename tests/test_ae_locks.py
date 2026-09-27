import pytest

from keepframe.after_effects.locks import resolve_locked_source_ids
from keepframe.ir.schema import Background, Constraint, Element, Group, Keyframe, Canonical, Scene, Track


def _scene(*constraints: Constraint) -> Scene:
    e1 = Element(
        id="e1",
        kind="sprite",
        canonical=Canonical(width=20, height=20),
        visible=(0, 59),
        tracks={
            "x": Track(keys=[Keyframe(t=0, v=0), Keyframe(t=20, v=100)]),
            "y": Track(keys=[Keyframe(t=0, v=50)]),
        },
    )
    e2 = Element(
        id="e2",
        kind="sprite",
        canonical=Canonical(width=20, height=20),
        visible=(0, 59),
        tracks={
            "x": Track(keys=[Keyframe(t=0, v=150)]),
            "y": Track(keys=[Keyframe(t=0, v=50)]),
            "rot": Track(keys=[Keyframe(t=30, v=0), Keyframe(t=40, v=45)]),
        },
    )
    return Scene(
        id="s",
        size=(200, 100),
        fps=30,
        frames=60,
        background=Background(),
        elements=[e1, e2],
        constraints=list(constraints),
    )


def test_resolves_motion_and_spatial_references_to_sorted_source_ids():
    scene = _scene(
        Constraint(pred="type(m_e1_1,'translation')", keep=True),
        Constraint(pred="dir(m_e1_1,[1,0])", keep=True),
        Constraint(pred="mag(m_e1_1,100)", keep=True),
        Constraint(pred="dur(m_e1_1,20)", keep=True),
        Constraint(pred="before(m_e1_1,m_e2_1)", keep=True),
        Constraint(pred="after(m_e2_1,m_e1_1)", keep=True),
        Constraint(pred="while(m_e1_1,m_e2_1)", keep=True),
        Constraint(pred="left(e1,e2)", keep=True),
        Constraint(pred="right(e1,e2)", keep=True),
        Constraint(pred="top(e1,e2)", keep=True),
        Constraint(pred="bottom(e1,e2)", keep=True),
        Constraint(pred="intersect(e1,e2)", keep=True),
    )

    assert resolve_locked_source_ids(scene) == ("e1", "e2")


def test_disabled_constraints_are_excluded_and_references_are_deduplicated():
    scene = _scene(
        Constraint(pred="type(m_e1_1,'translation')", keep=True),
        Constraint(pred="type(m_e1_1,'translation')", keep=True),
        Constraint(pred="left(e1,e2)", keep=False),
    )

    assert resolve_locked_source_ids(scene) == ("e1",)


def test_locks_nested_group_ancestors_without_locking_unrelated_groups():
    scene = _scene(Constraint(pred="type(m_e1_1,'translation')", keep=True)).model_copy(
        update={
            "groups": [
                Group(id="child", members=["e1"]),
                Group(id="parent", members=["child"]),
                Group(id="grandparent", members=["parent"]),
                Group(id="unrelated", members=["e2"]),
            ]
        }
    )

    assert resolve_locked_source_ids(scene) == (
        "child",
        "e1",
        "grandparent",
        "parent",
    )

def test_direct_group_reference_locks_group_ancestors():
    scene = _scene(Constraint(pred="left(child,e2)", keep=True)).model_copy(
        update={
            "groups": [
                Group(id="child", members=["e1"]),
                Group(id="parent", members=["child"]),
            ]
        }
    )

    assert resolve_locked_source_ids(scene) == ("child", "e2", "parent")


def test_rejects_group_cycles_and_overlapping_memberships():
    cyclic = _scene(Constraint(pred="type(m_e1_1,'translation')", keep=True)).model_copy(
        update={
            "groups": [
                Group(id="a", members=["b"]),
                Group(id="b", members=["a"]),
            ]
        }
    )
    overlapping = _scene(Constraint(pred="type(m_e1_1,'translation')", keep=True)).model_copy(
        update={
            "groups": [
                Group(id="a", members=["e1"]),
                Group(id="b", members=["e1"]),
            ]
        }
    )

    with pytest.raises(ValueError, match="group membership contains a cycle"):
        resolve_locked_source_ids(cyclic)
    with pytest.raises(ValueError, match="overlapping group membership"):
        resolve_locked_source_ids(overlapping)


@pytest.mark.parametrize(
    "predicate",
    [
        "dance(e1)",
        "left(e1)",
        "type(m_missing,'translation')",
        "left(e1,e_missing)",
    ],
)
def test_rejects_unknown_predicates_wrong_reference_counts_and_unknown_references(predicate):
    with pytest.raises(ValueError):
        resolve_locked_source_ids(_scene(Constraint(pred=predicate, keep=True)))
