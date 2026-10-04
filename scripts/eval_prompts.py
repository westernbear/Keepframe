"""Run fixed Korean/English prompts through the session agent on a copy of a project; report how many became valid typed edits."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

from keepframe.ir.schema import load_project_json
from keepframe.ir.store import current_scene
from keepframe.session.agent import SessionAgent
from keepframe.session.llm import AssistantReply, LLMClient, NullClient, make_llm
from keepframe.session.provider import load_llm_settings
from keepframe.session.tools import SessionContext

PROMPTS = [
    "제목 문구를 '가을 신상'으로 바꿔줘",
    "로고를 첨부한 이미지로 바꿔줘",
    "배경을 짙은 남색으로 바꿔줘",
    "전체를 1.5배 빠르게 해줘",
    "두 번째로 등장하는 텍스트를 0.3초 늦게 나오게 해줘",
    "제목 폰트를 더 굵게 해줘",
    "카드 색을 브랜드 컬러 #ff5a1f로 바꿔줘",
    "Change the headline to 'Fall Drop'",
]


class PreviewLLM:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def complete(self, messages, tools) -> AssistantReply:
        reply = self.llm.complete(messages, tools)
        calls = []
        for call in reply.tool_calls:
            args = call.get("arguments")
            if call.get("name") == "edit" and isinstance(args, dict):
                call = {**call, "arguments": {k: v for k, v in args.items() if k != "confirm"}}
            calls.append(call)
        return AssistantReply(content=reply.content, tool_calls=calls)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", required=True)
    ap.add_argument("--scene", default="s1")
    ap.add_argument("--workspace")
    a = ap.parse_args()
    workspace = Path(a.workspace) if a.workspace else None
    saved = load_llm_settings(workspace) if workspace is not None else None
    llm = make_llm(saved, workspace) if saved is not None else make_llm()
    if isinstance(llm, NullClient):
        print("Error: no LLM configured (NullClient); configure a provider before evaluation.", file=sys.stderr)
        return 2
    llm = PreviewLLM(llm)
    rows = []
    for prompt in PROMPTS:
        row = {"prompt": prompt, "ok": False, "typed": False, "calls": [], "results": [], "targets": [], "reply": ""}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "project"
                project = load_project_json((Path(a.project) / "project.json").read_text())
                # Legacy store loading snapshots stages into analysis before previewing.
                ignore = shutil.ignore_patterns("stages", "analysis") if project.analysis_migrated else None
                shutil.copytree(a.project, root, ignore=ignore)
                scene, _ = current_scene(root, a.scene)
                known = {e.id for e in scene.elements}
                ctx = SessionContext(root=root, scene_id=a.scene, has_attachment=prompt == PROMPTS[1])
                turn = SessionAgent(llm).turn(ctx, prompt, [])
                row.update(calls=turn.tool_calls, results=turn.results, reply=turn.reply[:200])
                edits = [(c, r) for c, r in zip(turn.tool_calls, turn.results) if c["name"] == "edit"]
                for call, result in edits:
                    chosen = (call.get("arguments") or {}).get("targets", [])
                    if not isinstance(chosen, list):
                        continue
                    valid = all(isinstance(t, dict) and (t.get("element") is None or isinstance(t.get("element"), str)) for t in chosen)
                    for target in chosen:
                        if not isinstance(target, dict):
                            continue
                        element = target.get("element")
                        row["targets"].append({
                            **target, "element": element, "property": target.get("property"), "value": target.get("value"),
                            "element_exists": isinstance(element, str) and element in known if element is not None else None,
                        })
                    row["ok"] = row["ok"] or bool(
                        valid and result.get("ok") is True and (result.get("needs_confirm") or result.get("needs_choice"))
                        and ((result.get("payload") or {}).get("intent") or {}).get("targets")
                    )
                row["typed"] = bool(row["targets"])
        except Exception as e:
            row.update(ok=False, error=f"{type(e).__name__}: {e}"[:300])
        rows.append(row)
    print(json.dumps({
        "ok": sum(r["ok"] for r in rows), "typed_ok": sum(r["ok"] and r["typed"] for r in rows),
        "n": len(rows), "rows": rows,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
