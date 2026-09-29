from __future__ import annotations

from ..ir.schema import Scene
from ..verify.matrix import Motion, extract_motions
from ..verify.predicates import parse_pred
from .mapping import _validate_groups

_MOTION_ONE_ARG = frozenset({"type", "dir", "mag", "dur"})
_MOTION_TWO_ARGS = frozenset({"before", "after", "while"})
_SPATIAL = frozenset({"left", "right", "top", "bottom", "intersect"})


def _require_args(name: str, args: list, count: int) -> None:
    if len(args) != count:
        raise ValueError(f"{name} expects {count} arguments, got {len(args)}")


def _motion_for(ref: object, motions: dict[str, Motion]) -> Motion:
    if not isinstance(ref, str) or ref not in motions:
        raise ValueError(f"unknown motion ID: {ref!r}")
    return motions[ref]


def _source_for(ref: object, source_ids: set[str]) -> str:
    if not isinstance(ref, str) or ref not in source_ids:
        raise ValueError(f"unknown source ID: {ref!r}")
    return ref


def resolve_locked_source_ids(scene: Scene) -> tuple[str, ...]:
    """Resolve enabled keep predicates and their transitive group ancestors."""
    source_ids = {element.id for element in scene.elements} | {group.id for group in scene.groups}
    motions = {motion.id: motion for motion in extract_motions(scene)}
    locked: set[str] = set()

    for constraint in scene.constraints:
        if not constraint.keep:
            continue
        name, args, _ = parse_pred(constraint.pred)

        if name in _MOTION_ONE_ARG:
            _require_args(name, args, 2)
            locked.add(_source_for(_motion_for(args[0], motions).element, source_ids))
        elif name in _MOTION_TWO_ARGS:
            _require_args(name, args, 2)
            for ref in args:
                locked.add(_source_for(_motion_for(ref, motions).element, source_ids))
        elif name in _SPATIAL:
            _require_args(name, args, 2)
            for ref in args:
                locked.add(_source_for(ref, source_ids))
        else:
            raise ValueError(f"unknown predicate: {name}")

    _, member_parent, _ = _validate_groups(
        scene,
        {element.id: element for element in scene.elements},
        {},
    )
    for source_id in tuple(locked):
        parent = member_parent.get(source_id)
        while parent is not None:
            locked.add(parent)
            parent = member_parent.get(parent)

    return tuple(sorted(locked))
