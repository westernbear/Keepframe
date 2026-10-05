from __future__ import annotations

from .schema import Element, Scene


def rank_elements(scene: Scene) -> list[Element]:
    """Rank observed text first, then canonical area times visible duration."""
    return sorted(scene.elements, key=lambda e: (
        not (e.kind == "text" and e.canonical.text),
        -e.canonical.width * e.canonical.height * (e.visible[1] - e.visible[0] + 1),
        e.visible[0], e.id,
    ))
