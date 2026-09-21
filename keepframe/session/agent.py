from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .llm import AssistantReply, LLMClient, make_llm
from .tools import TOOL_SCHEMAS, SessionContext, run_tool

MAX_STEPS = 8

SYSTEM = (
    "너는 Keepframe 세션 에이전트다. 사용자의 자연어 지시를 도구 호출 순서로 바꿔 실행하고 결과를 설명한다.\n"
    "규칙:\n"
    "- 측정·렌더·검증·편집을 직접 하지 말고 반드시 도구를 호출한다. 각 도구 내부는 결정적이다.\n"
    "- 재시도 상한 4회, 에셋 생성 상한 2회는 도구가 강제한다. 초과 시 실패로 보고한다.\n"
    "- keep 술어 검사를 생략하지 않는다. 충돌은 사용자 확인 없이 자동 처리하지 않는다.\n"
    "- 편집(edit)은 실행 전 해석을 확인받아야 하므로 먼저 confirm 없이 호출해 의도를 보여주고, 사용자가 확인하면 confirm을 true로 다시 호출한다.\n"
    "- 결과는 한국어로 간결하게 설명한다."
)


@dataclass
class SessionTurn:
    reply: str = ""
    status: str = "done"  # done | pending | error
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    needs_confirm: bool = False
    needs_choice: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "reply": self.reply,
            "status": self.status,
            "tool_calls": self.tool_calls,
            "results": self.results,
            "needs_confirm": self.needs_confirm,
            "needs_choice": self.needs_choice,
        }


def _scene_summary(ctx: SessionContext) -> str:
    try:
        from ..ir.store import current_scene, load_project, load_scene

        if ctx.version:
            project = load_project(ctx.root)
            v = next(x for x in project.versions if x.id == ctx.version and x.scene_file.startswith(f"scenes/{ctx.scene_id}/"))
            scene = load_scene(ctx.root / v.scene_file)
        else:
            scene, _ = current_scene(ctx.root, ctx.scene_id)
        els = []
        for e in scene.elements:
            els.append(f"{e.id}({e.kind}{':' + e.canonical.text if e.canonical.text else ''})")
        keep = [c.pred for c in scene.constraints if c.keep]
        return (
            f"장면 {scene.id}: {scene.frames}프레임, {scene.size[0]}x{scene.size[1]}.\n"
            f"요소: {', '.join(els) or '없음'}.\n"
            f"keep 조건: {', '.join(keep) or '없음'}."
        )
    except Exception:  # noqa: BLE001
        return ""


class SessionAgent:
    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm if llm is not None else make_llm()

    def turn(self, ctx: SessionContext, user_message: str, history: list[dict[str, Any]]) -> SessionTurn:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM},
            {"role": "system", "content": _scene_summary(ctx)},
        ]
        messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        turn = SessionTurn()
        for _ in range(MAX_STEPS):
            reply: AssistantReply = self.llm.complete(messages, TOOL_SCHEMAS)
            if not reply.tool_calls:
                turn.reply = reply.content
                return turn

            messages.append(
                {
                    "role": "assistant",
                    "content": reply.content or None,
                    "tool_calls": [
                        {
                            "id": tc.get("id"),
                            "type": "function",
                            "function": {"name": tc.get("name"), "arguments": json.dumps(tc.get("arguments") or {}, ensure_ascii=False)},
                        }
                        for tc in reply.tool_calls
                    ],
                }
            )

            for tc in reply.tool_calls:
                name, args = tc.get("name"), tc.get("arguments") or {}
                result = run_tool(name, ctx, args)
                turn.tool_calls.append({"name": name, "arguments": args})
                turn.results.append(result)
                messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": json.dumps(result, ensure_ascii=False)})
                if result.get("needs_confirm") or result.get("needs_choice"):
                    turn.status = "pending"
                    turn.needs_confirm = bool(result.get("needs_confirm"))
                    turn.needs_choice = bool(result.get("needs_choice"))
                    turn.reply = result.get("message", "")
                    return turn

        turn.reply = reply.content or "작업을 마쳤습니다."
        return turn
