from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

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


def _ok(message: str, **payload: Any) -> dict[str, Any]:
    return {"ok": True, "message": message, "needs_confirm": False, "needs_choice": False, "payload": payload}


def _pending(message: str, *, confirm: bool = False, choice: bool = False, **payload: Any) -> dict[str, Any]:
    return {"ok": True, "message": message, "needs_confirm": confirm, "needs_choice": choice, "payload": payload}


def _fail(message: str) -> dict[str, Any]:
    return {"ok": False, "message": message, "needs_confirm": False, "needs_choice": False, "payload": {}}


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
    scene, _ = _current(ctx)
    targets = set(args.get("targets") or [])
    on = bool(args.get("on", True))
    touched = 0
    for c in scene.constraints:
        if c.pred in targets or any(t in c.pred for t in targets):
            c.keep = on
            touched += 1
    if touched == 0:
        return _fail("대상과 일치하는 keep 조건을 찾지 못했습니다.")
    v = new_version(ctx.root, ctx.scene_id, scene, note=f"keep {'on' if on else 'off'} {len(targets)} targets", auto=False)
    return _ok(f"keep 조건 {touched}개를 {'유지' if on else '해제'}했습니다.", version=v.model_dump())


def _edit(ctx: SessionContext, args: dict[str, Any]) -> dict[str, Any]:
    from ..edit.agent import edit as run_edit

    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return _fail("edit에는 prompt가 필요합니다.")
    result = run_edit(
        ctx.root,
        ctx.scene_id,
        prompt,
        element=args.get("element"),
        confirm=bool(args.get("confirm")),
        intent=args.get("intent"),
        choices=args.get("choices"),
        version=ctx.version,
    )
    if result.status == "done" and result.version is not None:
        return _ok(result.summary or "편집 완료", version=result.version.model_dump(), verify=result.verify.model_dump() if result.verify else None)
    if result.status == "needs_confirm":
        return _pending(result.summary or "실행 전 확인이 필요합니다.", confirm=True, intent=result.intent.model_dump(), plan=result.plan.model_dump() if result.plan else None)
    if result.status == "needs_choice":
        return _pending(result.summary or "충돌 선택지가 필요합니다.", choice=True, intent=result.intent.model_dump(), plan=result.plan.model_dump() if result.plan else None)
    return _fail(result.error or result.summary or "편집 실패")


def _render(ctx: SessionContext, args: dict[str, Any]) -> dict[str, Any]:
    from ..compose.composer import compose

    if ctx.submit_job is None:
        return _fail("render는 작업 큐가 필요합니다.")
    sd = scene_dir(ctx.root, ctx.scene_id)
    scene, version = _current(ctx)
    html = sd / "composition.agent.html"
    compose(scene, sd, html)
    job = ctx.submit_job(
        "render",
        {
            "scene": str(ctx.root / version.scene_file),
            "html": str(html),
            "out": str(sd / "render-agent"),
            "mp4": bool(args.get("mp4", True)),
        },
        "render",
    )
    return _ok("렌더 작업을 큐에 넣었습니다.", job=job)


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
            "layer_max_err_px": rep.layer_max_err_px,
            "temporal": rep.temporal,
            "passed": rep.passed,
            "messages": rep.messages,
        },
    )


def _export(ctx: SessionContext, args: dict[str, Any]) -> dict[str, Any]:
    if ctx.submit_job is None:
        return _fail("export는 작업 큐가 필요합니다.")
    sd = scene_dir(ctx.root, ctx.scene_id)
    _, version = _current(ctx)
    job = ctx.submit_job(
        "export",
        {"scene": str(ctx.root / version.scene_file), "out": str(sd / "export-agent")},
        "export",
    )
    return _ok("내보내기 작업을 큐에 넣었습니다.", job=job)


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
        {"targets": {"type": "array", "items": {"type": "string"}}, "on": {"type": "boolean"}},
        ["targets"],
    ),
    _fn(
        "edit",
        "문구·색·이미지를 교체하는 편집을 해석하고 실행한다.",
        {"prompt": {"type": "string"}, "element": {"type": "string"}, "confirm": {"type": "boolean"}, "intent": {"type": "object"}, "choices": {"type": "object"}},
        ["prompt"],
    ),
    _fn("render", "장면을 렌더(프레임/MP4)한다.", {"mp4": {"type": "boolean"}}, []),
    _fn("verify", "현재 장면을 검증(스키마·keep 술어·프레임 비교)한다.", {}, []),
    _fn("export", "MP4와 프로젝트 zip을 내보낸다.", {"target": {"type": "string"}}, []),
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
