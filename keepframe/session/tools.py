from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..analyze.constraints import KEEP_PRESETS
from ..ir.store import current_scene, scene_dir
from ..log import describe as describe_exc, get
from . import web

log = get("keepframe.session")


@dataclass
class SessionContext:
    root: Path
    scene_id: str
    version: str | None = None
    workspace: Path | None = None
    project_id: str | None = None
    jobs: Any = None
    submit_job: Callable[[str, dict, str], dict] | None = None
    prepare_render: Callable[[str, str, str | None], Any] | None = None
    has_attachment: bool = False


def _ok(message: str, **payload: Any) -> dict[str, Any]:
    return {"ok": True, "message": message, "needs_confirm": False, "needs_choice": False, "payload": payload}


def _pending(message: str, *, confirm: bool = False, choice: bool = False, **payload: Any) -> dict[str, Any]:
    return {"ok": True, "message": message, "needs_confirm": confirm, "needs_choice": choice, "payload": payload}


def _fail(message: str) -> dict[str, Any]:
    return {"ok": False, "message": message, "needs_confirm": False, "needs_choice": False, "payload": {}}


def _json_payload(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _current(ctx: SessionContext):
    if ctx.version:
        from ..ir.store import load_project, load_scene

        project = load_project(ctx.root)
        v = next(x for x in project.versions if x.id == ctx.version and x.scene_file.startswith(f"scenes/{ctx.scene_id}/"))
        return load_scene(ctx.root / v.scene_file), v
    return current_scene(ctx.root, ctx.scene_id)


def _analyze(ctx: SessionContext, args: dict[str, Any]) -> dict[str, Any]:
    if ctx.submit_job is None:
        return _fail("analyze는 작업 큐가 필요합니다.")
    job = ctx.submit_job("analyze", args, "frames")
    return _ok("분석 작업을 큐에 넣었습니다.", job=job)


# (allowed, required) per op; mirrors ALLOWED/REQUIRED in frontend/src/lib/agent-corrections.ts
_CORRECT_ARGS = {
    "text": (("element_id", "text", "font"), ("element_id",)),
    "reassign": (("from_id", "to_id", "frames"), ("from_id", "to_id", "frames")),
    "mask": (("object_id", "frame", "mask_png_base64"), ("object_id", "frame", "mask_png_base64")),
    "bbox": (("object_id", "frame", "bbox"), ("object_id", "frame", "bbox")),
}
_FONT_FAMILY = re.compile(r"[A-Za-z0-9 \-가-힣]{1,64}")


def _correction_problem(scene, op: str, a: Any) -> str | None:
    """Why the browser preview would refuse these args (the same checks as validateShownCorrection), or None.
    Said here, the model can fix its call in the same turn instead of the user seeing a bare refusal."""
    allowed, required = _CORRECT_ARGS[op]
    if not isinstance(a, dict):
        return "args는 객체여야 합니다."
    extra, missing = sorted(set(a) - set(allowed)), [k for k in required if k not in a]
    if extra or missing:
        return f"{op} args는 {', '.join(allowed)}만 받습니다(필수: {', '.join(required)}). 잘못된 키: {extra or '없음'}, 빠진 키: {missing or '없음'}"
    ids = {e.id for e in scene.elements}
    for k in ("element_id", "object_id", "from_id", "to_id"):
        if k in a and a[k] not in ids:
            return f"{k} {a[k]!r}는 장면에 없는 요소입니다. 장면 브리프의 요소 id(e1, e2 …)를 쓰세요."
    last = scene.frames - 1
    frame_ok = lambda f: type(f) is int and 0 <= f <= last  # noqa: E731
    if op == "text":
        font = a.get("font")
        if "text" not in a and font is None:
            return "text 보정에는 text나 font가 필요합니다."
        if "text" in a and not isinstance(a["text"], str):
            return "text는 문자열이어야 합니다."
        if font is not None and (not isinstance(font, dict) or not font or set(font) - {"family_guess", "weight", "size_px"}
                                 or ("family_guess" in font and not (isinstance(font["family_guess"], str) and _FONT_FAMILY.fullmatch(font["family_guess"].strip())))
                                 or ("weight" in font and type(font["weight"]) is not int)
                                 or ("size_px" in font and not (isinstance(font["size_px"], (int, float)) and font["size_px"] > 0))):
            return "font는 family_guess(영문·숫자·한글·공백·-, 64자 이하), weight(정수), size_px(양수) 중 일부만 담은 객체여야 합니다."
    elif op == "reassign":
        f = a["frames"]
        if not (isinstance(f, list) and len(f) == 2 and all(map(frame_ok, f)) and f[0] <= f[1]):
            return f"frames는 [시작, 끝] 정수 프레임이어야 합니다(0–{last})."
    else:
        if not frame_ok(a["frame"]):
            return f"frame은 0–{last} 사이 정수여야 합니다."
        if op == "mask" and not (isinstance(a["mask_png_base64"], str) and a["mask_png_base64"].startswith("iVBORw0KGgo")):
            return "mask_png_base64는 장면 크기의 흑백 PNG를 base64로 담아야 합니다."
        b, (w, h) = a.get("bbox"), scene.size
        if op == "bbox" and not (isinstance(b, list) and len(b) == 4 and all(type(v) is int for v in b)
                                 and 0 <= b[0] < b[2] <= w and 0 <= b[1] < b[3] <= h):
            return f"bbox는 장면 안의 정수 [x0, y0, x1, y1]이어야 합니다(크기 {w}x{h}, x0<x1, y0<y1)."
    return None


def _correct(ctx: SessionContext, args: dict[str, Any]) -> dict[str, Any]:
    op = args.get("op")
    if op not in _CORRECT_ARGS:
        return _fail(f"지원하지 않는 보정 연산 {op!r}입니다. reassign/mask/bbox/text 중 하나를 쓰세요.")
    correction_args = args.get("args", {})
    try:
        scene, _ = _current(ctx)
    except Exception:  # noqa: BLE001 - no scene to check against; the browser preview still validates
        log.exception("correct: scene unavailable scene=%s", ctx.scene_id)
    else:
        problem = _correction_problem(scene, op, correction_args)
        if problem:
            return _fail(f"보정({op}) 인수가 잘못됐습니다: {problem}")
    return _pending(
        f"보정({op}) 미리보기를 준비했습니다. 브라우저에서 확인하면 실행됩니다.",
        confirm=True,
        correction={"op": op, "args": correction_args},
    )


def _set_keep(ctx: SessionContext, args: dict[str, Any]) -> dict[str, Any]:
    preset = args.get("preset")
    if "preset" in args and not isinstance(preset, str):
        return _fail("preset은 문자열이어야 합니다.")
    requested_targets = args.get("targets", [])
    if (
        not isinstance(requested_targets, list)
        or len(requested_targets) > 200
        or any(not isinstance(t, str) or not t or len(t) > 200 for t in requested_targets)
    ):
        return _fail("targets는 비어 있지 않은 문자열 목록이어야 합니다(최대 200개, 각 200자 이하).")
    if preset is not None:
        if preset not in KEEP_PRESETS:
            return _fail(f"unknown keep preset {preset!r}")
        return _pending(
            f"keep 프리셋 {preset} 미리보기를 준비했습니다. 브라우저에서 확인하면 적용됩니다.",
            confirm=True,
            keep_change={"preset": preset},
        )
    scene, _ = _current(ctx)
    targets = set(requested_targets)
    on = bool(args.get("on", True))
    predicates = [c.pred for c in scene.constraints if c.pred in targets or any(t in c.pred for t in targets)]
    matched = len(predicates)
    if matched == 0:
        return _fail("대상과 일치하는 keep 조건을 찾지 못했습니다.")
    return _pending(
        f"keep 조건 {matched}개를 {'유지' if on else '해제'}할 미리보기입니다. 브라우저에서 확인하면 적용됩니다.",
        confirm=True,
        keep_change={"targets": requested_targets, "on": on, "matched": matched, "examples": predicates[:5]},
    )


def _edit(ctx: SessionContext, args: dict[str, Any]) -> dict[str, Any]:
    from pydantic import ValidationError

    from ..edit.agent import edit as run_edit
    from ..edit.intent import SCENE_LEVEL, Intent, describe

    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return _fail("edit에는 prompt가 필요합니다.")
    targets = args.get("targets")
    intent = None
    if targets:
        scene, _ = _current(ctx)
        try:
            parsed = Intent.model_validate({"targets": targets})
        except ValidationError as e:
            error = e.errors()[0]
            loc = error["loc"]
            field = "targets"
            if len(loc) > 1:
                field += f"[{loc[1]}]." + (".".join(str(part) for part in loc[2:]) or "value")
            return _fail(f"targets 형식 오류: {field}: {error['msg']}")
        known = {e.id for e in scene.elements}
        if any(not t.element and t.property not in SCENE_LEVEL for t in parsed.targets):
            return _fail(f"대상 요소가 없습니다. 사용 가능한 id: {sorted(known)}")
        unknown = sorted({t.element for t in parsed.targets if t.element and t.element not in known})
        if unknown:
            return _fail(f"없는 요소 {unknown}. 사용 가능한 id: {sorted(known)}")
        parsed.summary = describe(parsed.targets, has_attachment=ctx.has_attachment)
        intent = parsed.model_dump()
    result = run_edit(
        ctx.root,
        ctx.scene_id,
        prompt,
        element=args.get("element"),
        confirm=False,
        intent=intent,
        version=ctx.version,
        has_attachment=ctx.has_attachment,
    )
    facts = {"background": result.background} if result.background else {}   # values for the agent, no prose
    if result.status == "needs_confirm":
        return _pending(result.summary or "실행 전 확인이 필요합니다.", confirm=True, intent=result.intent.model_dump(), plan=result.plan.model_dump() if result.plan else None, **facts)
    if result.status == "needs_choice":
        return _pending(result.summary or "충돌 선택지가 필요합니다.", choice=True, intent=result.intent.model_dump(), plan=result.plan.model_dump() if result.plan else None, **facts)
    return _fail(result.error or result.summary or "편집 실패")


def _render(ctx: SessionContext, args: dict[str, Any]) -> dict[str, Any]:
    backend = args.get("backend")
    if backend not in ("native",):
        return _fail("render에는 backend가 필요합니다(native).")
    direction = args.get("direction")
    if direction is not None and not isinstance(direction, str):
        return _fail("direction은 문자열이어야 합니다.")
    if ctx.prepare_render is None:
        return _fail("render 계획을 준비할 수 없습니다.")
    plan = ctx.prepare_render("preview", backend, direction)
    if plan is None:
        return _fail("render 계획을 준비할 수 없습니다.")
    return _pending(
        "렌더 계획을 준비했습니다. 브라우저에서 승인하면 실행됩니다.",
        confirm=True,
        render_plan=_json_payload(plan),
    )


def _verify(ctx: SessionContext, args: dict[str, Any]) -> dict[str, Any]:
    from ..verify.verifier import verify

    scene, _ = _current(ctx)
    sd = scene_dir(ctx.root, ctx.scene_id)
    rep = verify(scene, sd)
    return _ok(
        f"검증 {'통과' if rep.passed else '실패'} (keep {rep.keep_pass_rate:.0%}, 최대 오차 {rep.layer_max_err_px:.2f}px)",
        verify={
            "schema_ok": rep.schema_ok,
            "keep_pass_rate": rep.keep_pass_rate,
            "keep_total": rep.keep_total,
            "keep_failed": rep.keep_failed,
            "keep_failures": [r for r in rep.keep_results if not r["passed"]][:20],
            "layer_max_err_px": rep.layer_max_err_px,
            "temporal": rep.temporal,
            "passed": rep.passed,
            "messages": rep.messages,
        },
    )


def _export(ctx: SessionContext, args: dict[str, Any]) -> dict[str, Any]:
    backend = args.get("backend")
    if backend not in ("native", "lottie"):
        return _fail("export에는 backend가 필요합니다(native 또는 lottie).")
    direction = args.get("direction")
    if direction is not None and not isinstance(direction, str):
        return _fail("direction은 문자열이어야 합니다.")
    if ctx.prepare_render is None:
        return _fail("내보내기 계획을 준비할 수 없습니다.")
    plan = ctx.prepare_render("final", backend, direction)
    if plan is None:
        return _fail("내보내기 계획을 준비할 수 없습니다.")
    return _pending(
        "내보내기 계획을 준비했습니다. 브라우저에서 승인하면 실행됩니다.",
        confirm=True,
        render_plan=_json_payload(plan),
    )


def _report(ctx: SessionContext, args: dict[str, Any]) -> dict[str, Any]:
    scene, _ = _current(ctx)
    sd = scene_dir(ctx.root, ctx.scene_id)
    report_path = sd / "report.json"
    if not report_path.is_file():
        return _ok(f"요소 {len(scene.elements)}개. report.json이 없어 측정 지표는 생략합니다.", elements=[e.id for e in scene.elements])
    data = json.loads(report_path.read_text(encoding="utf-8"))
    conf = data.get("confidence") or {}
    summary = {
        "elements": len(scene.elements),
        "fit_error_max_px": data.get("fit_error_max_px"),
        "reconstruction": (data.get("reconstruction") or {}).get("mean_l1"),
        "confidence": {k: v for k, v in conf.items()},
        "messages": data.get("messages") or [],
    }
    return _ok(f"요소 {summary['elements']}개, 최대 오차 {summary['fit_error_max_px']}px.", report=summary)


def _web_search(ctx: SessionContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        results = web.search(args.get("query", ""), args.get("max_results") or 5)
    except web.WebError as e:
        return _fail(str(e))
    return _ok(f"검색 결과 {len(results)}개.", results=results)


def _web_fetch(ctx: SessionContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        page = web.fetch(args.get("url", ""), args.get("max_length") or 5000, args.get("start_index") or 0)
    except web.WebError as e:
        return _fail(str(e))
    return _ok(f"{page['url']} 본문 {len(page['content'])}자.", **page)


TOOLS: dict[str, Callable[[SessionContext, dict[str, Any]], dict[str, Any]]] = {
    "analyze": _analyze,
    "correct": _correct,
    "set_keep": _set_keep,
    "edit": _edit,
    "render": _render,
    "verify": _verify,
    "export": _export,
    "report": _report,
    "web_search": _web_search,
    "web_fetch": _web_fetch,
}


def _fn(name: str, desc: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


def _edit_params() -> dict[str, Any]:
    from ..edit.intent import Intent, Target

    item = Target.model_json_schema()
    item.pop("title", None)
    defs = item.pop("$defs", {})

    def inline(node):
        if isinstance(node, dict):
            if "$ref" in node:
                node = {**defs[node["$ref"].removeprefix("#/$defs/")], **{k: v for k, v in node.items() if k != "$ref"}}
            return {k: inline(v) for k, v in node.items()}
        if isinstance(node, list):
            return [inline(v) for v in node]
        return node

    return {
        "prompt": {"type": "string", "description": "사용자 원문 요청"},
        "targets": {"type": "array", "maxItems": Intent.model_json_schema()["properties"]["targets"]["maxItems"],
                    "items": inline(item), "description": "장면 브리프의 요소 id로 해석한 변경 목록"},
        "element": {"type": "string", "description": "프롬프트 해석에 사용할 장면 브리프의 요소 id(예: e12). 장면 id 's1'을 넣지 않는다."},
        "confirm": {"type": "boolean", "description": "무시되는 값이다. 실행은 브라우저의 확인 버튼으로만 일어난다."},
        "choices": {"type": "object", "additionalProperties": {"type": "string"}, "description": "충돌 선택 제안. 무시되는 값이며 선택은 사용자가 브라우저에서 한다."},
    }


TOOL_SCHEMAS: list[dict[str, Any]] = [
    _fn("analyze", "레퍼런스 영상을 IR 장면으로 (재)분석한다.", {"mode": {"type": "string"}}, []),
    _fn(
        "correct",
        "검수 보정 4연산(reassign/mask/bbox/text) 중 하나의 미리보기만 준비한다. 실행은 사용자가 브라우저에서 확인한 뒤에 일어난다.",
        {"op": {"type": "string", "enum": ["reassign", "mask", "bbox", "text"]}, "args": {
            "type": "object",
            "description": "op별 키만 넣는다. text: element_id(필수), text 또는 font. reassign: from_id, to_id, frames. "
                           "mask: object_id, frame, mask_png_base64. bbox: object_id, frame, bbox. 다른 키를 넣으면 거부된다.",
            "properties": {
                "element_id": {"type": "string", "description": "text: 고칠 텍스트 요소 id(예: e3)"},
                "text": {"type": "string"},
                "font": {"type": "object", "properties": {"family_guess": {"type": "string"}, "weight": {"type": "integer"}, "size_px": {"type": "number"}}},
                "from_id": {"type": "string"}, "to_id": {"type": "string"},
                "frames": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2, "description": "[시작, 끝] 프레임"},
                "object_id": {"type": "string"},
                "frame": {"type": "integer"},
                "bbox": {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4, "description": "[x0, y0, x1, y1] 픽셀"},
                "mask_png_base64": {"type": "string"},
            },
        }},
        ["op"],
    ),
    _fn(
        "set_keep",
        "요소나 keep 술어의 유지 여부 변경 미리보기만 준비한다. 적용은 사용자가 브라우저에서 확인한 뒤에 일어난다.",
        {"targets": {"type": "array", "maxItems": 200, "items": {"type": "string", "minLength": 1, "maxLength": 200}}, "on": {"type": "boolean"}, "preset": {"type": "string", "enum": sorted(KEEP_PRESETS)}},
        [],
    ),
    _fn(
        "edit",
        "문구·색·이미지 등의 편집 해석과 계획만 준비한다. 실행은 브라우저의 확인 버튼으로만 일어난다. 가능하면 targets를 채운다. "
        "A background colour edit replaces the whole background, a picture, gradient or video included, with that flat "
        "colour unless mode is \"tint\" (keeps a picture's or gradient's light and dark), and a PNG attached with "
        "value \"attachment\" becomes the new background picture; the result's `background` facts give the "
        "background before and after.",
        _edit_params(),
        ["prompt"],
    ),
    _fn(
        "render",
        "backend를 지정해 렌더 계획만 준비한다. 실행은 사용자가 브라우저에서 승인할 때만 일어난다.",
        {
            "backend": {"type": "string", "enum": ["native"]},
            "direction": {"type": "string"},
            "confirm": {"type": "boolean", "description": "무시되는 값이며 실행 권한을 주지 않는다."},
        },
        ["backend"],
    ),
    _fn("verify", "현재 장면을 검증(스키마·keep 술어·프레임 비교)한다.", {}, []),
    _fn(
        "export",
        "backend를 지정해 최종 내보내기 계획만 준비한다. 실행은 사용자가 브라우저에서 승인할 때만 일어난다.",
        {
            "backend": {"type": "string", "enum": ["native", "lottie"]},
            "direction": {"type": "string"},
            "confirm": {"type": "boolean", "description": "무시되는 값이며 실행 권한을 주지 않는다."},
        },
        ["backend"],
    ),
    _fn("report", "재구성 오차·요소 신뢰도 리포트를 요약한다.", {}, []),
    _fn(
        "web_search",
        "웹을 검색해 제목·URL·요약을 돌려준다. 폰트 이름, 효과 기법, AE 사용법처럼 장면 밖의 사실이 필요할 때만 쓴다.",
        {"query": {"type": "string", "description": "검색어"},
         "max_results": {"type": "integer", "minimum": 1, "maximum": 10, "description": "결과 수(기본 5)"}},
        ["query"],
    ),
    _fn(
        "web_fetch",
        "공개 http(s) URL의 본문을 텍스트로 가져온다. 길면 next_start를 start_index로 넘겨 이어 읽는다.",
        {"url": {"type": "string", "description": "가져올 URL"},
         "max_length": {"type": "integer", "minimum": 1, "maximum": web.MAX_LENGTH, "description": "돌려받을 최대 글자 수(기본 5000)"},
         "start_index": {"type": "integer", "minimum": 0, "description": "이어 읽을 시작 위치(기본 0)"}},
        ["url"],
    ),
]


def run_tool(name: str, ctx: SessionContext, args: dict[str, Any]) -> dict[str, Any]:
    handler = TOOLS.get(name)
    if handler is None:
        return _fail(f"알 수 없는 도구 {name!r}입니다.")
    try:
        return handler(ctx, args or {})
    except Exception as e:  # noqa: BLE001 - tool boundary: surface any failure to the LLM
        log.warning("tool %s failed: %s", name, describe_exc(e, trace=True))
        return _fail(f"tool_failed ({type(e).__name__})")
