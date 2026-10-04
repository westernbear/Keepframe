"""Run fixed Korean/English prompts through the session agent on a copy of a project; report how many became valid typed edits."""
from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path

from keepframe.ir.store import current_scene
from keepframe.session.agent import SessionAgent
from keepframe.session.llm import make_llm
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", required=True)
    ap.add_argument("--scene", default="s1")
    ap.add_argument("--workspace")
    a = ap.parse_args()
    workspace = Path(a.workspace) if a.workspace else None
    saved = load_llm_settings(workspace) if workspace is not None else None
    llm = make_llm(saved, workspace) if saved is not None else make_llm()
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for prompt in PROMPTS:
            root = Path(tmp) / f"p{len(rows)}"
            shutil.copytree(a.project, root)
            scene, _ = current_scene(root, a.scene)
            known = {e.id for e in scene.elements}
            ctx = SessionContext(root=root, scene_id=a.scene, has_attachment=prompt == PROMPTS[1])
            turn = SessionAgent(llm).turn(ctx, prompt, [])
            edits = [(c, r) for c, r in zip(turn.tool_calls, turn.results) if c["name"] == "edit"]
            ok = any(
                r.get("ok") is True and (r.get("needs_confirm") or r.get("needs_choice"))
                and ((r.get("payload") or {}).get("intent") or {}).get("targets")
                for _, r in edits
            )
            targets = []
            for call, _ in edits:
                chosen = (call.get("arguments") or {}).get("targets") or []
                for target in chosen if isinstance(chosen, list) else []:
                    if isinstance(target, dict):
                        element = target.get("element")
                        targets.append({
                            **target, "element": element, "property": target.get("property"), "value": target.get("value"),
                            "element_exists": element in known if element else None,
                        })
            rows.append({
                "prompt": prompt, "ok": ok, "calls": turn.tool_calls, "results": turn.results,
                "targets": targets, "reply": turn.reply[:200],
            })
    print(json.dumps({"ok": sum(r["ok"] for r in rows), "n": len(rows), "rows": rows}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
