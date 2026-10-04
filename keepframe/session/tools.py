from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..analyze.constraints import KEEP_PRESETS, apply_keep_preset
from ..ir.store import current_scene, new_version, scene_dir
from ..log import get

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


def _correct(ctx: SessionContext, args: dict[str, Any]) -> dict[str, Any]:
    op = args.get("op")
    if op not in ("reassign", "mask", "bbox", "text"):
        return _fail(f"지원하지 않는 보정 연산 {op!r}입니다. reassign/mask/bbox/text 중 하나를 쓰세요.")
    if ctx.submit_job is None:
        return _fail("correct는 작업 큐가 필요합니다.")
    job = ctx.submit_job("correct", {"op": op, "args": args.get("args", {})}, "correct")
    return _ok(f"보정({op}) 작업을 큐에 넣었습니다.", job=job)


def _set_keep(ctx: SessionContext, args: dict[str, Any]) -> dict[str, Any]:
    preset = args.get("preset")
    if preset is not None:
        scene, parent = _current(ctx)
        try:
            scene.constraints = apply_keep_preset(scene.constraints, str(preset))
        except ValueError as e:
            return _fail(str(e))
        v = new_version(ctx.root, ctx.scene_id, scene, note=f"keep preset {preset}", auto=False, parent_version=parent.id)
        return _ok(f"keep 프리셋 {preset}을 적용했습니다.", version=v.model_dump())
    scene, parent = _current(ctx)
    targets = set(args.get("targets") or [])
    on = bool(args.get("on", True))
    touched = 0
    for c in scene.constraints:
        if c.pred in targets or any(t in c.pred for t in targets):
            c.keep = on
            touched += 1
    if touched == 0:
        return _fail("대상과 일치하는 keep 조건을 찾지 못했습니다.")
    v = new_version(ctx.root, ctx.scene_id, scene, note=f"keep {'on' if on else 'off'} {len(targets)} targets", auto=False, parent_version=parent.id)
    return _ok(f"keep 조건 {touched}개를 {'유지' if on else '해제'}했습니다.", version=v.model_dump())


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
    if result.status == "needs_confirm":
        return _pending(result.summary or "실행 전 확인이 필요합니다.", confirm=True, intent=result.intent.model_dump(), plan=result.plan.model_dump() if result.plan else None)
    if result.status == "needs_choice":
        return _pending(result.summary or "충돌 선택지가 필요합니다.", choice=True, intent=result.intent.model_dump(), plan=result.plan.model_dump() if result.plan else None)
    return _fail(result.error or result.summary or "편집 실패")


def _render(ctx: SessionContext, args: dict[str, Any]) -> dict[str, Any]:
    backend = args.get("backend")
    if backend not in ("native", "after_effects"):
        return _fail("render에는 backend가 필요합니다(native 또는 after_effects).")
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
            "keep_results": rep.keep_results,
            "layer_max_err_px": rep.layer_max_err_px,
            "temporal": rep.temporal,
            "passed": rep.passed,
            "messages": rep.messages,
        },
    )


def _export(ctx: SessionContext, args: dict[str, Any]) -> dict[str, Any]:
    backend = args.get("backend")
    if backend not in ("native", "after_effects", "lottie"):
        return _fail("export에는 backend가 필요합니다(native, after_effects 또는 lottie).")
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


TOOLS: dict[str, Callable[[SessionContext, dict[str, Any]], dict[str, Any]]] = {
    "analyze": _analyze,
    "correct": _correct,
    "set_keep": _set_keep,
    "edit": _edit,
    "render": _render,
    "verify": _verify,
    "export": _export,
    "report": _report,
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
        "confirm": {"type": "boolean", "description": "권한이 아니며 무시된다. 실행은 브라우저의 확인 버튼으로만 이뤄진다."},
        "choices": {"type": "object", "additionalProperties": {"type": "string"}, "description": "충돌 선택 제안. 실행 권한이 아니며 무시된다. 사용자가 브라우저에서 선택한다."},
    }


TOOL_SCHEMAS: list[dict[str, Any]] = [
    _fn("analyze", "레퍼런스 영상을 IR 장면으로 (재)분석한다.", {"mode": {"type": "string"}}, []),
    _fn(
        "correct",
        "검수 보정 4연산(reassign/mask/bbox/text) 중 하나를 실행한다.",
        {"op": {"type": "string", "enum": ["reassign", "mask", "bbox", "text"]}, "args": {"type": "object"}},
        ["op"],
    ),
    _fn(
        "set_keep",
        "요소나 keep 술어의 유지 여부를 켜고 끈다.",
        {"targets": {"type": "array", "items": {"type": "string"}}, "on": {"type": "boolean"}, "preset": {"type": "string", "enum": sorted(KEEP_PRESETS)}},
        [],
    ),
    _fn(
        "edit",
        "문구·색·이미지 등의 편집 해석과 계획만 준비한다. 실행은 브라우저의 확인 버튼으로만 이뤄진다. 가능하면 targets를 채운다.",
        _edit_params(),
        ["prompt"],
    ),
    _fn(
        "render",
        "backend를 지정해 렌더 계획만 준비한다. 실행은 브라우저의 명시적 승인으로만 이뤄진다.",
        {
            "backend": {"type": "string", "enum": ["native", "after_effects"]},
            "direction": {"type": "string"},
            "confirm": {"type": "boolean", "description": "권한이 아니며 무시된다."},
        },
        ["backend"],
    ),
    _fn("verify", "현재 장면을 검증(스키마·keep 술어·프레임 비교)한다.", {}, []),
    _fn(
        "export",
        "backend를 지정해 최종 내보내기 계획만 준비한다. 실행은 브라우저의 명시적 승인으로만 이뤄진다.",
        {
            "backend": {"type": "string", "enum": ["native", "after_effects", "lottie"]},
            "direction": {"type": "string"},
            "confirm": {"type": "boolean", "description": "권한이 아니며 무시된다."},
        },
        ["backend"],
    ),
    _fn("report", "재구성 오차·요소 신뢰도 리포트를 요약한다.", {}, []),
]


def run_tool(name: str, ctx: SessionContext, args: dict[str, Any]) -> dict[str, Any]:
    handler = TOOLS.get(name)
    if handler is None:
        return _fail(f"알 수 없는 도구 {name!r}입니다.")
    try:
        return handler(ctx, args or {})
    except Exception as e:  # noqa: BLE001 - tool boundary: surface any failure to the LLM
        log.exception("tool %s failed", name)
        return _fail(f"{type(e).__name__}: {e}")
