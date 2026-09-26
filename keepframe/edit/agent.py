from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..compose.composer import compose
from ..render.renderer import render
from ..ir.schema import Scene, Version
from ..ir.store import current_scene, load_project, load_scene, new_version, scene_dir
from ..verify.verifier import VerifyReport, verify
from .apply import apply_edit
from .intent import Intent, Plan, interpret, plan

MAX_TRIES = 4
ASSET_GEN_CAP = 2
KEEP_MIN = 0.95
TEMPORAL_MIN = 0.7


class EditResult(BaseModel):
    status: str
    summary: str = ""
    intent: Intent | None = None
    plan: Plan | None = None
    version: Version | None = None
    verify: VerifyReport | None = None
    attempts: int = 0
    error: str | None = None
    messages: list[str] = Field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def _passed(rep: VerifyReport) -> bool:
    temporal_ok = rep.temporal is None or rep.temporal >= TEMPORAL_MIN
    return bool(rep.schema_ok and rep.keep_pass_rate >= KEEP_MIN and rep.layer_probe_complete and rep.passed and temporal_ok)


def _load(root: Path, scene_id: str, version: str | None) -> tuple[Scene, Version]:
    if not version:
        return current_scene(root, scene_id)
    project = load_project(root)
    v = next(x for x in project.versions if x.id == version and x.scene_file.startswith(f"scenes/{scene_id}/"))
    return load_scene(Path(root) / v.scene_file), v


def edit(
    root: Path,
    scene_id: str,
    prompt: str,
    *,
    attachment: str | Path | bytes | None = None,
    element: str | None = None,
    confirm: bool = False,
    intent: Intent | dict | None = None,
    choices: dict[str, str] | None = None,
    version: str | None = None,
) -> EditResult:
    root = Path(root)
    scene, parent = _load(root, scene_id, version)
    sd = scene_dir(root, scene_id)
    parsed = Intent.model_validate(intent) if intent is not None else interpret(
        prompt, scene, element=element, has_attachment=attachment is not None
    )
    built = plan(scene, parsed)
    if parsed.ambiguous or not parsed.targets:
        return EditResult(status="failed", summary=parsed.summary, intent=parsed, plan=built, error=parsed.summary or "ambiguous")
    if not confirm:
        return EditResult(status="needs_confirm", summary=parsed.summary, intent=parsed, plan=built)
    missing = [c for c in built.conflicts if not (choices or {}).get(c.id) and not (choices or {}).get(c.element)]
    if missing:
        return EditResult(status="needs_choice", summary=parsed.summary, intent=parsed, plan=built)

    last_rep: VerifyReport | None = None
    choices_map = dict(choices or {})
    asset_uses: dict[str, int] = {}
    attempt = 0
    for attempt in range(1, MAX_TRIES + 1):
        for target in built.items:
            key = f"{target.element}:{target.property}"
            if asset_uses.get(key, 0) >= ASSET_GEN_CAP:
                return EditResult(
                    status="failed",
                    summary=parsed.summary,
                    intent=parsed,
                    plan=built,
                    verify=last_rep,
                    attempts=attempt,
                    error="에셋 생성 상한(2회)을 초과했습니다.",
                    messages=(last_rep.messages if last_rep else []),
                )
        edited = apply_edit(scene, sd, built.items, choices_map, attachment)
        for target in built.items:
            key = f"{target.element}:{target.property}"
            asset_uses[key] = asset_uses.get(key, 0) + 1
        html = compose(edited, sd, sd / f"composition.edit{attempt}.html")
        probes = render(html, edited, sd / f"render.edit{attempt}")
        last_rep = verify(edited, sd, render_result=probes, reference=scene, reference_dir=sd)
        if _passed(last_rep):
            v = new_version(root, scene_id, edited, note=parsed.summary or prompt, auto=True)
            return EditResult(
                status="done",
                summary=parsed.summary,
                intent=parsed,
                plan=built,
                version=v,
                verify=last_rep,
                attempts=attempt,
                messages=last_rep.messages,
            )
        break
    return EditResult(
        status="failed",
        summary=parsed.summary,
        intent=parsed,
        plan=built,
        verify=last_rep,
        attempts=attempt,
        error="keep 검증을 통과하지 못했습니다.",
        messages=(last_rep.messages if last_rep else []),
    )
