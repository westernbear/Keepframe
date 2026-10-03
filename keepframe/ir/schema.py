from __future__ import annotations
import json
import math
import re
from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Ease = tuple[float, float, float, float]
PROPS = ("x", "y", "sx", "sy", "rot", "skx", "sky", "opacity")
DEFAULTS: dict[str, float] = {"x": 0.0, "y": 0.0, "sx": 1.0, "sy": 1.0, "rot": 0.0, "skx": 0.0, "sky": 0.0, "opacity": 1.0}


class Keyframe(BaseModel):
    t: int
    v: float
    ease: Optional[Ease] = None  # easing from this key to the next; None = linear


class Track(BaseModel):
    keys: list[Keyframe]

    @field_validator("keys")
    @classmethod
    def _sorted_unique(cls, keys: list[Keyframe]) -> list[Keyframe]:
        if not keys:
            raise ValueError("track needs at least one keyframe")
        ts = [k.t for k in keys]
        if ts != sorted(ts) or len(set(ts)) != len(ts):
            raise ValueError("keyframes must be sorted by t and unique")
        return keys


class FontGuess(BaseModel):
    family_guess: str = "sans-serif"
    weight: int = 400
    size_px: float = 32.0


class Canonical(BaseModel):
    width: float
    height: float
    anchor: tuple[float, float] = (0.5, 0.5)
    texture: Optional[str] = None
    text: Optional[str] = None
    font: Optional[FontGuess] = None
    color: Optional[str] = None
    model: Optional[str] = None


class FitError(BaseModel):
    max_px: float = 0.0
    max_frames: int = 0


def _zero_track() -> Track:
    return Track(keys=[Keyframe(t=0, v=0.0)])


class Element(BaseModel):
    id: str
    kind: Literal["text", "sprite", "ui", "3d", "group"]
    role: Literal["primary", "secondary", "text", "background"] = "secondary"
    canonical: Canonical
    visible: tuple[int, int]
    tracks: dict[str, Track] = Field(default_factory=dict)
    z: Track = Field(default_factory=_zero_track)
    raw: Optional[str] = None
    fit_error: FitError = Field(default_factory=FitError)
    confidence: float = 1.0
    provenance: Literal["auto", "manual"] = "auto"
    label: Optional[str] = None     # VLM suggestion (logo/title/...); never used for timing or geometry
    caption: Optional[str] = None   # VLM description; data, not instructions

    @field_validator("tracks")
    @classmethod
    def _known_props(cls, tracks: dict[str, Track]) -> dict[str, Track]:
        bad = set(tracks) - set(PROPS)
        if bad:
            raise ValueError(f"unknown track properties: {sorted(bad)}")
        return tracks

    @model_validator(mode="after")
    def _visible_range(self):
        a, b = self.visible
        if a > b:
            raise ValueError("visible[0] must be <= visible[1]")
        return self


class Group(BaseModel):
    id: str
    members: list[str]
    reason: str = ""


class Constraint(BaseModel):
    pred: str
    keep: bool = False


class Background(BaseModel):
    kind: Literal["color", "image"] = "color"
    value: str = "#000000"
    confidence: float = 1.0


class UIStateRange(BaseModel):
    frames: tuple[int, int]
    state: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _ordered_frames(self):
        if self.frames[0] > self.frames[1]:
            raise ValueError("UI state frame range must be ordered")
        return self


class UIComponent(BaseModel):
    id: str
    kind: Literal["nav", "card", "button", "list", "table", "chart", "generic"]
    bbox: tuple[float, float, float, float]
    text: Optional[str] = None
    props: dict = Field(default_factory=dict)
    children: list["UIComponent"] = Field(default_factory=list)
    states: list[UIStateRange] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _safe_id(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            raise ValueError("UI component id is invalid")
        return value

    @field_validator("bbox")
    @classmethod
    def _valid_bbox(cls, bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        x0, y0, x1, y1 = bbox
        if not all(math.isfinite(value) for value in bbox) or x0 > x1 or y0 > y1:
            raise ValueError("UI bbox must be ordered")
        return bbox


class UIModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    schema_version: str = Field("keepframe.ui/1", alias="schema")
    components: list[UIComponent] = Field(default_factory=list)


class Scene(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    schema_version: str = Field("keepframe.scene/1", alias="schema")
    id: str
    size: tuple[int, int]
    fps: float
    frames: int
    background: Background
    elements: list[Element]
    groups: list[Group] = Field(default_factory=list)
    constraints: list[Constraint] = Field(default_factory=list)
    ui: Optional[UIModel] = None

    def element(self, eid: str) -> Element:
        for e in self.elements:
            if e.id == eid:
                return e
        raise KeyError(eid)


class SceneRef(BaseModel):
    id: str
    frames: tuple[int, int]
    transition_out: Optional[dict] = None


class Version(BaseModel):
    id: str
    parent: Optional[str] = None
    note: str = ""
    auto: bool = True
    scene_file: str
    analysis_file: Optional[str] = None


class Project(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    schema_version: str = Field("keepframe.project/1", alias="schema")
    source: dict
    analysis_migrated: bool = False
    scenes: list[SceneRef]
    links: list[dict] = Field(default_factory=list)
    versions: list[Version] = Field(default_factory=list)
    approved_scenes: dict[str, str] = Field(default_factory=dict)


def dump(model: BaseModel) -> str:
    return json.dumps(model.model_dump(by_alias=True), indent=2, sort_keys=True)


def load_scene_json(text: str) -> Scene:
    return Scene.model_validate(json.loads(text))


def load_project_json(text: str) -> Project:
    return Project.model_validate(json.loads(text))
