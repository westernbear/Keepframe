from __future__ import annotations
import math
from typing import TYPE_CHECKING
from ..ir.schema import Element, Keyframe, Scene, Track

if TYPE_CHECKING:
    from .intent import Target

MAX_SCENE_SECONDS = 120

# ponytail: retime keys and the visible range; z and raw measurements stay untouched (raw is the reference's L0).


def _remap(t: int, speed: float, delay_frames: int, anchor: int) -> int:
    return int(round(anchor + (t - anchor) / speed)) + delay_frames


def _dedupe(keys: list[Keyframe]) -> list[Keyframe]:
    by_t: dict[int, Keyframe] = {}
    for k in keys:
        by_t[k.t] = k            # collapsed keys: the later key wins
    return [by_t[t] for t in sorted(by_t)]


def retimed_range(el: Element, speed: float, delay_frames: int, anchor: int) -> tuple[int, int]:
    return _remap(el.visible[0], speed, delay_frames, anchor), _remap(el.visible[1], speed, delay_frames, anchor)


def retime_element(el: Element, speed: float, delay_frames: int, anchor: int) -> None:
    start, end = retimed_range(el, speed, delay_frames, anchor)
    if start < 0:
        raise ValueError(f"timing moves {el.id} before the scene start")
    for name, track in el.tracks.items():
        el.tracks[name] = Track(keys=_dedupe([k.model_copy(update={"t": _remap(k.t, speed, delay_frames, anchor)}) for k in track.keys]))
    el.visible = (start, max(start, end))


def retime_scene(scene: Scene, speed: float) -> None:
    for el in scene.elements:
        retime_element(el, speed, 0, 0)
    scene.frames = math.ceil((scene.frames - 1) / speed) + 1
    _retime_ui(scene, speed, 0, 0)


def timing_args(t: Target, el: Element | None, fps: float) -> tuple[float, int, int]:
    return t.speed or 1.0, round((t.delay or 0) * fps), el.visible[0] if el is not None else 0


def _retime_ui(scene: Scene, speed: float, delay_frames: int, anchor: int, element_id: str | None = None) -> None:
    components = list(scene.ui.components) if scene.ui is not None else []
    while components:
        component = components.pop()
        components.extend(component.children)
        if element_id is None or component.id == element_id:
            for state in component.states:
                state.frames = tuple(max(0, min(scene.frames - 1, _remap(t, speed, delay_frames, anchor))) for t in state.frames)


def apply_timing(scene: Scene, targets: list[Target], choices: dict[str, str] | None) -> None:
    choice_of = choices or {}
    for t in targets:
        if t.property != "timing":
            continue
        el = scene.element(t.element) if t.element is not None else None
        speed, delay_frames, anchor = timing_args(t, el, scene.fps)
        if el is None:
            retime_scene(scene, speed)
        else:
            retime_element(el, speed, delay_frames, anchor)
            if el.visible[1] > scene.frames - 1:
                if (choice_of.get("timing_overflow") or choice_of.get(el.id)) != "extend_scene":
                    raise ValueError("timing_overflow needs a choice")
                scene.frames = el.visible[1] + 1
            _retime_ui(scene, speed, delay_frames, anchor, el.id)
        if scene.frames > math.ceil(MAX_SCENE_SECONDS * scene.fps):
            raise ValueError(f"timing would make the scene longer than {MAX_SCENE_SECONDS}s")
