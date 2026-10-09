from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from ..ir.schema import Element, FontGuess, Scene, validate_font_family
from .retime import apply_timing, retimed_range, timing_args
from .textraster import resolve_families

Prop = Literal["text", "color", "texture", "model", "background", "font", "timing"]
SCENE_LEVEL: frozenset[str] = frozenset({"background", "timing"})
COLOR_NAMES: dict[str, str] = {"흰색": "#ffffff", "하얀": "#ffffff", "white": "#ffffff", "검정": "#000000", "검은": "#000000", "black": "#000000",
                              "빨간": "#e53935", "빨강": "#e53935", "red": "#e53935", "파란": "#1e66f5", "파랑": "#1e66f5", "blue": "#1e66f5",
                              "초록": "#2e7d32", "green": "#2e7d32", "노란": "#fdd835", "노랑": "#fdd835", "yellow": "#fdd835",
                              "회색": "#9e9e9e", "gray": "#9e9e9e", "주황": "#fb8c00", "orange": "#fb8c00", "보라": "#8e24aa", "purple": "#8e24aa",
                              "녹색": "#2e7d32", "그린": "#2e7d32", "블루": "#1e66f5", "레드": "#e53935", "화이트": "#ffffff", "블랙": "#000000",
                              "옐로": "#fdd835", "퍼플": "#8e24aa", "오렌지": "#fb8c00", "그레이": "#9e9e9e"}
_HEX_FULL = re.compile(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})")
BACKGROUND_CHOICES = ("tint", "replace", "cancel")   # a colour on a picture/gradient/video background (D5)
_BG_KIND_KO = {"image": "그림", "gradient": "그라데이션"}


class Target(BaseModel):
    element: str | None = Field(default=None, description="장면 브리프의 요소 id(예: e12). background와 장면 전체 timing에는 비워 둔다(장면 id 's1'을 넣지 않는다)")
    property: Prop = Field(description="허용된 편집: text(문구), color(요소 색), texture(이미지), model(3D 모델), background(배경색), font(폰트), timing(속도·지연).")
    value: str | None = Field(default=None, max_length=500, description="text: 새 문구, color/background: #rrggbb, font: 폰트 패밀리, texture: 생성 설명 또는 'attachment', model: 참조 크롭으로 생성하려면 'reference', 새 생성 설명 또는 'attachment'.")
    weight: int | None = Field(default=None, ge=100, le=900, description="폰트 굵기(100~900, 400=보통, 700=굵게).")
    speed: float | None = Field(default=None, gt=0.1, le=10, description="timing 속도 배율(1=원래 속도, 2=2배 빠르게).")
    delay: float | None = Field(default=None, ge=-30, le=30, description="timing 지연 시간(초). 요소별 timing에만 사용하며 음수는 앞당긴다.")

    @model_validator(mode="after")
    def _shape(self):
        if self.element is not None and not self.element.strip():
            self.element = None
        if self.property == "text" and (not self.value or not self.value.strip()):
            raise ValueError("text value is required")
        if self.property == "background" and self.element is not None:
            raise ValueError("background targets have no element")
        if self.property == "timing":
            if self.speed is None and self.delay is None:
                raise ValueError("timing needs speed or delay")
            if self.element is None and self.delay is not None:
                raise ValueError("delay needs an element")
        if self.property in ("color", "background"):
            if not self.value or not _HEX_FULL.fullmatch(self.value):
                raise ValueError("color value must be #rrggbb")
            self.value = _norm_hex(self.value)
        if self.property == "font":
            if self.value is not None:
                self.value = self.value.strip() or None
            if not (self.value or self.weight):
                raise ValueError("font needs a family or weight")
            if self.value:
                self.value = validate_font_family(self.value)
        return self


class Intent(BaseModel):
    targets: list[Target] = Field(default_factory=list, max_length=16)
    summary: str = ""
    ambiguous: bool = False
    candidates: list[str] = Field(default_factory=list)


class Conflict(BaseModel):
    id: str
    element: str
    choices: list[str]
    reason: str = ""


class Plan(BaseModel):
    items: list[Target] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)


_ELEM = re.compile(r"\b(e\d+)\b", re.I)
_HEX = re.compile(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![0-9a-fA-F])")
_TEXT_KO = re.compile(r"(?:문구|텍스트|글|카피)(?:를|을)\s*[「\"'](.+?)[」\"']")
_TEXT_KO_BARE = re.compile(r"(?:문구|텍스트|글|카피)(?:를|을)\s+(\S+?)(?:으로|로)(?:\s|$)")
_TEXT_SWAP = re.compile(r"[「\"']([^\"'」]+)[」\"']\s*(?:를|을)\s*[「\"']([^\"'」]+)[」\"']\s*(?:으로|로)")
_TEXT_EN = re.compile(r"(?:change|set|replace)\s+(?:the\s+)?text\s+(?:to|with)\s+[\"']?(.+?)[\"']?\s*$", re.I)
_IMAGE = re.compile(r"(이미지|사진|텍스처|교체|replace(?:\s+the)?\s+image|swap(?:\s+image)?|texture)", re.I)
_MODEL = re.compile(r"(3d|3차원|모델|glb)", re.I)
_REFERENCE_MODEL = re.compile(
    r"^(?:(?:e\d+|이\s*(?:요소|객체))(?:을|를)?\s*)?(?:3d|3차원)\s*(?:모델)?(?:으로|로)?\s*"
    r"(?:바꿔(?:줘)?|교체(?:해(?:줘)?)?|생성(?:해(?:줘)?)?|만들어(?:줘)?)\s*[.!?]?$"
    r"|^(?:convert|change|replace)\s+(?:this\s+)?(?:element|object|e\d+)\s+(?:to|with)\s+"
    r"(?:a\s+)?3d(?:\s+model)?\s*[.!?]?$", re.I,
)
_BG = re.compile(r"(배경|background)", re.I)
_QUOTED = re.compile(r"「[^」]*」|\"[^\"]*\"|'[^']*'")
_COLOR_CUE = re.compile(r"(색|컬러|colou?r|배경|background)", re.I)
_CLAUSE = re.compile(r"[,;.!?\n]|\b(?:and|then)\b|하고|그리고", re.I)


def _norm_hex(h: str) -> str:
    h = h.lower()
    if len(h) == 4:
        return "#" + "".join(c * 2 for c in h[1:])
    return h


def _color_in(prompt: str) -> str | None:
    prompt = _QUOTED.sub(" ", prompt)
    hexes = _HEX.findall(prompt)
    if hexes:
        return _norm_hex(hexes[-1])
    if not _COLOR_CUE.search(prompt):
        return None
    low = prompt.lower()
    for name, value in COLOR_NAMES.items():
        if re.search(rf"\b{re.escape(name)}\b", low) if name.isascii() else name in low:
            return value
    return None


def _texts(scene: Scene) -> list[Element]:
    return [e for e in scene.elements if e.kind == "text" or e.canonical.text]


def _sprites(scene: Scene) -> list[Element]:
    return [e for e in scene.elements if e.kind == "sprite"]


def _pick(cands: list[Element], hinted: str | None, selected: str | None) -> tuple[str | None, list[str]]:
    ids = [e.id for e in cands]
    if hinted and hinted in ids:
        return hinted, []
    if selected and selected in ids:
        return selected, []
    if len(ids) == 1:
        return ids[0], []
    return None, ids


def background_mode(scene: Scene | None, choices: dict | None) -> str | None:
    """How a background colour edit applies: None for a colour background (the colour replaces it), else the
    chosen 'tint' / 'replace' / 'cancel', or '' while nothing is chosen."""
    if scene is None or scene.background.kind == "color":
        return None
    choices = choices or {}
    return choices.get("background_kind") or choices.get("background_video") or choices.get("background") or ""


def describe(targets: list[Target], has_attachment: bool = False, *, scene: Scene | None = None,
             choices: dict | None = None) -> str:
    """The edit in one Korean sentence. A colour for a picture/gradient/video background says how it applies:
    asks before the choice, then names the chosen tint or replace."""
    bits, after = [], ""
    mode = background_mode(scene, choices)
    for t in targets:
        who = t.element or "?"
        if t.property == "text":
            bits.append(f"{who} 문구를 {t.value or ''}(으)로")
        elif t.property == "color":
            bits.append(f"{who} 색을 {t.value or ''}로")
        elif t.property == "background" and mode:
            after = (f"배경에 {t.value or ''} 색을 입힙니다(밝고 어두운 결 유지)." if mode == "tint"
                     else f"배경을 {t.value or ''} 단색으로 바꿉니다." if mode == "replace" else "")
        elif t.property == "background" and mode == "":
            after = f"배경에 {t.value or ''} 적용 — 방식을 고르세요."
        elif t.property == "background":
            bits.append(f"배경색을 {t.value or ''}로")
        elif t.property == "font":
            bits.append(f"{who} 폰트를 {t.value or ''} {t.weight or ''}".rstrip() + "로")
        elif t.property == "timing":
            bits.append(f"{t.element or '장면 전체'} 속도 ×{t.speed or 1:g} 지연 {t.delay or 0:g}s로")
        elif t.property == "model":
            bits.append(f"{who} 3D 모델을 {'첨부로' if has_attachment or t.value == 'attachment' else '생성해'}")
        else:
            bits.append(f"{who} 이미지를 {'첨부로' if has_attachment or t.value == 'attachment' else '생성해'}")
    head = " ".join(bits) + " 바꿉니다." if bits else ""
    return " ".join(x for x in (head, after) if x) + " 트랙은 유지합니다."


def interpret(prompt: str, scene: Scene, *, element: str | None = None, has_attachment: bool = False) -> Intent:
    prompt = (prompt or "").strip()
    hinted = (_ELEM.search(prompt).group(1).lower() if _ELEM.search(prompt) else None)
    targets: list[Target] = []
    color_prompt = _QUOTED.sub(" ", prompt)
    if (_BG.search(color_prompt) and _ELEM.search(color_prompt)
            and any(_COLOR_CUE.search(_BG.split(clause, maxsplit=1)[0]) for clause in _CLAUSE.split(color_prompt))):
        return Intent(ambiguous=True, summary="배경과 요소 색을 함께 바꿀 수 없습니다. 하나씩 요청하세요.")

    swap = _TEXT_SWAP.search(prompt)
    if swap:
        old, new = swap.group(1), swap.group(2)
        match = next((e for e in _texts(scene) if e.canonical.text == old), None)
        eid, cands = _pick(_texts(scene), match.id if match else hinted, element)
        targets.append(Target(element=eid, property="text", value=new))
        if eid is None:
            return Intent(targets=targets, summary=f"문구를 {new}(으)로 바꿉니다. 요소를 고르세요.", ambiguous=True, candidates=cands)

    text_m = _TEXT_KO.search(prompt) or _TEXT_KO_BARE.search(prompt) or _TEXT_EN.search(prompt)
    if text_m and not swap:
        value = text_m.group(1).strip().rstrip(".,")
        eid, cands = _pick(_texts(scene), hinted, element)
        targets.append(Target(element=eid, property="text", value=value))
        if eid is None:
            return Intent(targets=targets, summary=f"문구를 {value}(으)로 바꿉니다. 요소를 고르세요.", ambiguous=True, candidates=cands)

    if _BG.search(color_prompt) and (c := _color_in(color_prompt)):
        targets.append(Target(property="background", value=c))
    elif c := _color_in(color_prompt):
        eid, cands = _pick(scene.elements, hinted, element)
        targets.append(Target(element=eid, property="color", value=c))
        if eid is None:
            return Intent(targets=targets, summary=f"색을 {c}로 바꿉니다. 요소를 고르세요.", ambiguous=True, candidates=cands)

    if _MODEL.search(prompt):
        eid, cands = _pick(scene.elements, hinted, element)
        # ponytail: recognise short conversion requests; extend typed intent parsing for richer phrasing.
        value = "attachment" if has_attachment else "reference" if _REFERENCE_MODEL.fullmatch(prompt) else prompt[:500]
        targets.append(Target(element=eid, property="model", value=value))
        if eid is None:
            return Intent(targets=targets, summary="3D 모델로 바꿉니다. 요소를 고르세요.", ambiguous=True, candidates=cands)
    elif (has_attachment and not targets) or _IMAGE.search(prompt):
        eid, cands = _pick(_sprites(scene) or scene.elements, hinted, element)
        targets.append(Target(element=eid, property="texture", value="attachment" if has_attachment else prompt[:500]))
        if eid is None:
            return Intent(targets=targets, summary="첨부 이미지로 텍스처를 바꿉니다. 요소를 고르세요.", ambiguous=True, candidates=cands)

    if not targets:
        return Intent(summary="문구·색·이미지 교체를 읽지 못했습니다.", ambiguous=True)

    return Intent(targets=targets, summary=describe(targets, has_attachment, scene=scene))


def plan(scene: Scene, intent: Intent, *, fonts=None, scene_dir=None) -> Plan:
    """`fonts`: the project's registry (uploads); bundled fonts only when None. `scene_dir` resolves pinned font
    files for the overflow measurement."""
    from ..fonts.registry import FontRegistry
    from .apply import text_width

    fonts = fonts or FontRegistry()
    items: list[Target] = []
    conflicts: list[Conflict] = []
    timing_scene = scene.model_copy(deep=True) if any(t.property == "timing" for t in intent.targets) else scene
    for t in intent.targets:
        if not t.element and t.property not in SCENE_LEVEL:
            continue
        source = timing_scene if t.property == "timing" else scene
        el = source.element(t.element) if t.element else None
        items.append(t)
        if t.property == "background" and scene.background.kind == "video":   # tint waits for Task 13 (R54)
            conflicts.append(Conflict(id="background_video", element="background", choices=["replace", "cancel"],
                                      reason="배경이 영상입니다. 영상에는 아직 색을 입힐 수 없습니다. 단색으로 바꿀까요?"))
        elif t.property == "background" and scene.background.kind != "color":
            conflicts.append(Conflict(id="background_kind", element="background", choices=list(BACKGROUND_CHOICES),
                                      reason=f"배경이 {_BG_KIND_KO.get(scene.background.kind, '그림')}입니다. "
                                             "색만 입힐까요(밝고 어두운 결 유지), 단색으로 바꿀까요?"))
        if t.property == "timing":
            if el is not None:
                _, end = retimed_range(el, *timing_args(t, el, timing_scene.fps))
                if end > timing_scene.frames - 1:
                    conflicts.append(Conflict(id="timing_overflow", element=el.id, choices=["extend_scene"],
                                              reason=f"장면 끝({timing_scene.frames}프레임)을 넘어 {end + 1}프레임까지 이어집니다."))
            apply_timing(timing_scene, [t], {"timing_overflow": "extend_scene"})
        if t.property == "text" and t.value and el is not None:
            if text_width(el, t.value, fonts=fonts, scene_dir=scene_dir) > el.canonical.width * 1.15:
                conflicts.append(Conflict(
                    id="overflow",
                    element=el.id,
                    choices=["shrink_font", "wrap", "expand_box"],
                    reason="새 문구가 원래 상자보다 깁니다.",
                ))
        if t.property == "font" and el is not None and el.canonical.text:
            base = el.canonical.font or FontGuess()
            update = {k: v for k, v in (("family_guess", t.value), ("weight", t.weight)) if v}
            if t.value and t.value.casefold() != base.family_guess.casefold():   # as apply_edit: the old file goes
                update.update(file=None, postscript=None, fallback=None, fallback_weight=None, fallback_scale=1.0)
            resolved = resolve_families(t.value) if t.value and not fonts.faces(t.value) else ()   # uploads/bundled first
            if t.value and resolved and t.value.casefold() not in {name.casefold() for name in resolved}:
                conflicts.append(Conflict(id="font_missing", element=el.id, choices=["use_fallback"],
                                          reason=f"{t.value} 폰트가 설치되어 있지 않습니다. {resolved[0]}(으)로 그려집니다."))
            new = base.model_copy(update=update)
            if text_width(el, el.canonical.text, new, fonts=fonts, scene_dir=scene_dir) > el.canonical.width * 1.15:
                conflicts.append(Conflict(id="overflow", element=el.id, choices=["shrink_font", "wrap", "expand_box"],
                                          reason="바꾼 폰트로는 문구가 원래 상자보다 깁니다."))
    return Plan(items=items, conflicts=conflicts)
