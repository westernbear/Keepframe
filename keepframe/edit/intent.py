from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from ..ir.schema import Element, Scene

Prop = Literal["text", "color", "texture"]


class Target(BaseModel):
    element: str | None = None
    property: Prop
    value: str | None = None


class Intent(BaseModel):
    targets: list[Target] = Field(default_factory=list)
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


def _norm_hex(h: str) -> str:
    h = h.lower()
    if len(h) == 4:
        return "#" + "".join(c * 2 for c in h[1:])
    return h


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


def interpret(prompt: str, scene: Scene, *, element: str | None = None, has_attachment: bool = False) -> Intent:
    prompt = (prompt or "").strip()
    hinted = (_ELEM.search(prompt).group(1).lower() if _ELEM.search(prompt) else None)
    targets: list[Target] = []

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

    hexes = _HEX.findall(prompt)
    if hexes:
        eid, cands = _pick(scene.elements, hinted, element)
        targets.append(Target(element=eid, property="color", value=_norm_hex(hexes[-1])))
        if eid is None:
            return Intent(targets=targets, summary=f"색을 {hexes[-1]}로 바꿉니다. 요소를 고르세요.", ambiguous=True, candidates=cands)

    if has_attachment and (_IMAGE.search(prompt) or not targets):
        eid, cands = _pick(_sprites(scene) or scene.elements, hinted, element)
        targets.append(Target(element=eid, property="texture", value="attachment"))
        if eid is None:
            return Intent(targets=targets, summary="첨부 이미지로 텍스처를 바꿉니다. 요소를 고르세요.", ambiguous=True, candidates=cands)

    if not targets:
        return Intent(summary="문구·색·이미지 교체를 읽지 못했습니다.", ambiguous=True)

    bits = []
    for t in targets:
        who = t.element or "?"
        if t.property == "text":
            bits.append(f"{who} 문구를 {t.value}(으)로")
        elif t.property == "color":
            bits.append(f"{who} 색을 {t.value}로")
        else:
            bits.append(f"{who} 이미지를 첨부로")
    return Intent(targets=targets, summary=" ".join(bits) + " 바꿉니다. 트랙은 유지합니다.")


def plan(scene: Scene, intent: Intent) -> Plan:
    from .apply import measure_text

    items: list[Target] = []
    conflicts: list[Conflict] = []
    for t in intent.targets:
        if not t.element:
            continue
        el = scene.element(t.element)
        items.append(t)
        if t.property == "text" and t.value:
            font = el.canonical.font
            w, _ = measure_text(t.value, font.size_px if font else 32.0)
            if w > el.canonical.width * 1.15:
                conflicts.append(Conflict(
                    id="overflow",
                    element=el.id,
                    choices=["shrink_font", "wrap", "expand_box"],
                    reason="새 문구가 원래 상자보다 깁니다.",
                ))
    return Plan(items=items, conflicts=conflicts)
