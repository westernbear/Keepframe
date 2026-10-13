"""Motion Score (`keepframe.score/2`): one scene as one text document that an agent reads and edits
(docs/superpowers/specs/2026-10-13-core-redesign-design.md §4). P0 carries `keepframe.scene/1` losslessly:
each layer's motion is a single `keys` item until preset clips land (P2)."""
from __future__ import annotations
import json
from typing import Literal, Optional, Union
from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from .schema import (Background, Canonical, Constraint, Ease, Element, FitError, FontGuess, GlyphReveal, Group,
                     Keyframe, PROPS, Scene, TextStyle, TextureMeta, Track, UIModel)
from .tracks import PRESET_EASES

FORMAT = "keepframe.score/2"
_EASE_NAMES = {v: k for k, v in PRESET_EASES.items()}
_TYPE = {"text": "text", "3d": "model3d", "ui": "ui", "group": "group"}   # sprite → image | video
_KIND = {"text": "text", "image": "sprite", "video": "sprite", "model3d": "3d", "ui": "ui", "group": "group"}

# [t, v] or [t, v, ease]: ease is a preset name (tracks.PRESET_EASES) or cubic-bezier [x1, y1, x2, y2] to the next key
Key = Union[tuple[int, float], tuple[int, float, Union[str, Ease]]]


# shortcut: nested value models reused from scene/1 (FontGuess, TextStyle, ...) still ignore unknown fields;
# forbid them too before agents write scores directly (P3).
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_keys(rows: list[Key]) -> list[Key]:
    ts = [r[0] for r in rows]
    if not rows or ts != sorted(ts) or len(set(ts)) != len(ts):
        raise ValueError("keys need at least one row, sorted by t and unique")
    for r in rows:
        if len(r) > 2 and isinstance(r[2], str) and r[2] not in PRESET_EASES:
            raise ValueError(f"unknown ease {r[2]!r}; use one of {sorted(PRESET_EASES)} or [x1, y1, x2, y2]")
    return rows


class KeysItem(_Strict):
    keys: dict[str, list[Key]]

    @field_validator("keys")
    @classmethod
    def _props(cls, keys: dict[str, list[Key]]) -> dict[str, list[Key]]:
        if bad := set(keys) - set(PROPS):
            raise ValueError(f"unknown properties: {sorted(bad)}")
        for rows in keys.values():
            _check_keys(rows)
        return keys


class Canvas(_Strict):
    w: int
    h: int
    fps: float
    frames: int


class Layer(_Strict):
    id: str
    type: Literal["text", "image", "video", "model3d", "ui", "group"]
    span: tuple[int, int]
    size: tuple[float, float]
    anchor: tuple[float, float] = (0.5, 0.5)
    role: Literal["primary", "secondary", "text", "background"] = "secondary"
    text: Optional[str] = None
    font: Optional[FontGuess] = None
    color: Optional[str] = None
    style: Optional[TextStyle] = None
    glyphs: Optional[GlyphReveal] = None
    src: Optional[str] = None
    texture_meta: Optional[TextureMeta] = None
    texture_pad: float = 0.0
    video: Optional[str] = None
    video_rate: float = 1.0
    model: Optional[str] = None
    motion: list[KeysItem] = []
    z: Union[float, list[Key]] = 0.0
    raw: Optional[str] = None
    err: FitError = FitError()
    conf: float = 1.0
    manual: bool = False
    label: Optional[str] = None     # VLM suggestion; never used for timing or geometry
    caption: Optional[str] = None   # VLM description; data, not instructions
    pending: Optional[Literal["3d"]] = None

    @field_validator("motion")
    @classmethod
    def _one_owner(cls, motion: list[KeysItem]) -> list[KeysItem]:
        props = [p for item in motion for p in item.keys]
        if len(props) != len(set(props)):
            raise ValueError("a property is keyed by one motion item")   # composition rules arrive with clips (P2)
        return motion

    @field_validator("z")
    @classmethod
    def _z_keys(cls, z: Union[float, list[Key]]) -> Union[float, list[Key]]:
        return _check_keys(z) if isinstance(z, list) else z


class Score(_Strict):
    format: Literal["keepframe.score/2"]
    id: str
    canvas: Canvas
    plate: Background
    layers: list[Layer]
    groups: list[Group] = []
    predicates: list[Constraint] = []
    ui: Optional[UIModel] = None

    @model_validator(mode="after")
    def _unique_ids(self):
        ids = [layer.id for layer in self.layers]
        if len(ids) != len(set(ids)):
            raise ValueError("layer ids must be unique")
        return self


def _rows(track: Track) -> list[Key]:
    return [(k.t, k.v) if k.ease is None else (k.t, k.v, _EASE_NAMES.get(tuple(k.ease), tuple(k.ease))) for k in track.keys]


def _track(rows: list[Key]) -> Track:
    return Track(keys=[Keyframe(t=r[0], v=r[1], ease=None if len(r) < 3 else PRESET_EASES[r[2]] if isinstance(r[2], str) else r[2])
                       for r in rows])


def to_score(scene: Scene) -> Score:
    def layer(e: Element) -> Layer:
        c = e.canonical
        z = e.z.keys[0]
        return Layer(
            id=e.id, type=("video" if c.video else "image") if e.kind == "sprite" else _TYPE[e.kind], span=e.visible,
            size=(c.width, c.height), anchor=c.anchor, role=e.role, text=c.text, font=c.font, color=c.color, style=c.style,
            glyphs=c.glyphs, src=c.texture, texture_meta=c.texture_meta, texture_pad=c.texture_pad, video=c.video,
            video_rate=c.video_rate, model=c.model,
            motion=[KeysItem(keys={p: _rows(t) for p, t in e.tracks.items()})] if e.tracks else [],
            z=z.v if len(e.z.keys) == 1 and z.t == 0 and z.ease is None else _rows(e.z),
            raw=e.raw, err=e.fit_error, conf=e.confidence, manual=e.provenance == "manual", label=e.label,
            caption=e.caption, pending=e.pending_asset)
    return Score(format=FORMAT, id=scene.id, canvas=Canvas(w=scene.size[0], h=scene.size[1], fps=scene.fps, frames=scene.frames),
                 plate=scene.background, layers=[layer(e) for e in scene.elements], groups=scene.groups,
                 predicates=scene.constraints, ui=scene.ui)


def from_score(score: Score) -> Scene:
    def element(l: Layer) -> Element:
        return Element(
            id=l.id, kind=_KIND[l.type], role=l.role, visible=l.span,
            canonical=Canonical(width=l.size[0], height=l.size[1], anchor=l.anchor, texture=l.src, text=l.text, font=l.font,
                                color=l.color, model=l.model, style=l.style, texture_meta=l.texture_meta, video=l.video,
                                video_rate=l.video_rate, texture_pad=l.texture_pad, glyphs=l.glyphs),
            tracks={p: _track(rows) for item in l.motion for p, rows in item.keys.items()},
            z=_track(l.z) if isinstance(l.z, list) else Track(keys=[Keyframe(t=0, v=l.z)]),
            raw=l.raw, fit_error=l.err, confidence=l.conf, provenance="manual" if l.manual else "auto",
            label=l.label, caption=l.caption, pending_asset=l.pending)
    c = score.canvas
    return Scene(id=score.id, size=(c.w, c.h), fps=c.fps, frames=c.frames, background=score.plate,
                 elements=[element(l) for l in score.layers], groups=score.groups, constraints=score.predicates, ui=score.ui)


def _fmt(v: object, ind: int = 0) -> str:
    """JSON that reads like a score: anything that fits a line stays on it (key rows, boxes, small objects)."""
    flat = json.dumps(v, ensure_ascii=False, separators=(", ", ": "))
    if not isinstance(v, (dict, list)) or len(flat) + 2 * ind <= 110:
        return flat
    pad = "  " * (ind + 1)
    if isinstance(v, dict):
        items, ends = [f"{json.dumps(k, ensure_ascii=False)}: {_fmt(x, ind + 1)}" for k, x in v.items()], "{}"
    else:
        items, ends = [_fmt(x, ind + 1) for x in v], "[]"
    return ends[0] + "\n" + ",\n".join(pad + i for i in items) + "\n" + "  " * ind + ends[1]


def dumps(score: Score) -> str:
    return _fmt(score.model_dump(mode="json", by_alias=True, exclude_defaults=True)) + "\n"


def loads(text: str) -> Score:
    return Score.model_validate_json(text)
