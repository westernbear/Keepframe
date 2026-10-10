from __future__ import annotations
import json
import math
import re
from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from ..log import get

Ease = tuple[float, float, float, float]
PROPS = ("x", "y", "sx", "sy", "rot", "skx", "sky", "opacity", "reveal", "rx", "ry")
DEFAULTS: dict[str, float] = {"x": 0.0, "y": 0.0, "sx": 1.0, "sy": 1.0, "rot": 0.0, "skx": 0.0, "sky": 0.0, "opacity": 1.0, "reveal": 1.0, "rx": 0.0, "ry": 0.0}
FONT_FAMILY_RE = re.compile(r"[A-Za-z0-9 \-가-힣]{1,64}")
POSTSCRIPT_RE = re.compile(r"[A-Za-z0-9._-]{1,63}")
HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})")


def _hex_colour(value: object) -> str:
    if not isinstance(value, str) or not HEX_RE.fullmatch(value):
        raise ValueError("colour must be #rgb or #rrggbb")
    value = value.lower()
    return "#" + "".join(c * 2 for c in value[1:]) if len(value) == 4 else value


def validate_font_family(value: object) -> str:
    """Normalize a font family from input, rejecting unsafe or empty names."""
    if isinstance(value, str):
        value = value.strip()
        if FONT_FAMILY_RE.fullmatch(value):
            return value
    raise ValueError("font family may only contain letters, digits, spaces and hyphens (1–64 characters)")


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
    candidates: list[str] = Field(default_factory=list)
    scores: list[float] = Field(default_factory=list)
    confidence: float = 1.0
    source: Literal["bundled", "uploaded", "system", "generic"] = "generic"
    file: Optional[str] = None
    postscript: Optional[str] = None
    fallback: Optional[str] = None
    fallback_weight: Optional[int] = None
    fallback_scale: float = 1.0

    @field_validator("postscript", mode="before")
    @classmethod
    def _safe_postscript(cls, value: object) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str) and POSTSCRIPT_RE.fullmatch(value):
            return value
        get("keepframe.ir").warning("invalid stored PostScript name; ignoring it")
        return None

    @field_validator("family_guess", mode="before")
    @classmethod
    def _safe_family(cls, value: object) -> str:
        try:
            return validate_font_family(value)
        except ValueError:
            get("keepframe.ir").warning("invalid stored font family; using sans-serif")
            return "sans-serif"


class GradientStop(BaseModel):
    offset: float = Field(ge=0.0, le=1.0)
    color: str

    @field_validator("color", mode="before")
    @classmethod
    def _colour(cls, value: object) -> str:
        return _hex_colour(value)


class Gradient(BaseModel):
    kind: Literal["linear", "radial"] = "linear"
    stops: list[GradientStop]
    angle: float = 180.0
    center: tuple[float, float] = (0.5, 0.5)
    radius: float = Field(1.0, gt=0.0)

    @field_validator("stops")
    @classmethod
    def _ordered_stops(cls, stops: list[GradientStop]) -> list[GradientStop]:
        if not 2 <= len(stops) <= 8:
            raise ValueError("a gradient needs 2 to 8 stops")
        if any(a.offset > b.offset for a, b in zip(stops, stops[1:])):
            raise ValueError("gradient stops must be in ascending offset order")
        return stops


class GradientKey(BaseModel):
    t: int
    gradient: Gradient


class TextureMeta(BaseModel):
    method: Literal["triangulation", "two_colour", "keyed", "binary"]
    frames: list[int] = Field(default_factory=list)
    confidence: float = 1.0
    padding: int = 0


class TextEffect(BaseModel):
    kind: Literal["stroke", "shadow", "glow"]
    color: str
    opacity: float = 1.0
    width: float = 0.0
    dx: float = 0.0
    dy: float = 0.0
    blur: float = 0.0


class AlphaStop(BaseModel):
    offset: float
    alpha: float


class Fade(BaseModel):
    angle: float = 180.0
    stops: list[AlphaStop]

    @field_validator("stops")
    @classmethod
    def _stop_count(cls, stops: list[AlphaStop]) -> list[AlphaStop]:
        if not 2 <= len(stops) <= 4:
            raise ValueError("a fade needs 2 to 4 stops")
        return stops


class TextStyle(BaseModel):
    fill: Optional[Gradient] = None
    fade: Optional[Fade] = None
    effects: list[TextEffect] = Field(default_factory=list)
    tracking_em: float = 0.0
    shear_deg: float = 0.0
    dx: float = 0.0
    dy: float = 0.0
    stroke_ratio: Optional[float] = None
    confidence: float = 1.0

    @field_validator("effects")
    @classmethod
    def _effect_count(cls, effects: list[TextEffect]) -> list[TextEffect]:
        if len(effects) > 4:
            raise ValueError("a text style takes at most 4 effects")
        return effects


class Canonical(BaseModel):
    width: float
    height: float
    anchor: tuple[float, float] = (0.5, 0.5)
    texture: Optional[str] = None
    text: Optional[str] = None
    font: Optional[FontGuess] = None
    color: Optional[str] = None
    model: Optional[str] = None
    style: Optional[TextStyle] = None
    texture_meta: Optional[TextureMeta] = None
    video: Optional[str] = None
    # A video sprite's playback rate (speed edits): frame f shows source frame floor((f − start) · rate). Absent when 1.
    video_rate: float = Field(default=1.0, gt=0.0, allow_inf_nan=False, exclude_if=lambda v: v == 1.0)
    # Styled text textures carry their effects past the box (R41): the texture covers the box grown by this many
    # box pixels on every side.
    texture_pad: float = Field(default=0.0, ge=0.0, le=512.0, exclude_if=lambda v: not v)   # absent when 0


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
    pending_asset: Optional[Literal["3d"]] = None

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
    kind: Literal["color", "image", "gradient", "video"] = "color"
    value: str = "#000000"
    confidence: float = 1.0
    gradient: Optional[Gradient] = None
    gradient_keys: list[GradientKey] = Field(default_factory=list)
    poster: Optional[str] = None
    synthetic: Optional[str] = None
    # A video background's playback rate (scene speed edits), as Canonical.video_rate. Absent when 1.
    video_rate: float = Field(default=1.0, gt=0.0, allow_inf_nan=False, exclude_if=lambda v: v == 1.0)

    @model_validator(mode="after")
    def _gradient_fields(self):
        if self.kind == "gradient" and self.gradient is None and not self.gradient_keys:
            raise ValueError("a gradient background needs a gradient")
        ts = [k.t for k in self.gradient_keys]
        if ts != sorted(ts) or len(set(ts)) != len(ts):
            raise ValueError("gradient keys must be sorted by t and unique")
        return self


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
