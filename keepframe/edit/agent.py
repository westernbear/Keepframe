from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
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
from ..assets import AssetAPIError, AssetClient

MAX_TRIES = 4
ASSET_GEN_CAP = 2
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
    return bool(rep.passed and temporal_ok)


def _load(root: Path, scene_id: str, version: str | None) -> tuple[Scene, Version]:
    if not version:
        return current_scene(root, scene_id)
    project = load_project(root)
    v = next(x for x in project.versions if x.id == version and x.scene_file.startswith(f"scenes/{scene_id}/"))
    return load_scene(Path(root) / v.scene_file), v


def _candidate_digest(scene: Scene, candidate_dir: Path) -> str:
    digest = hashlib.sha256(json.dumps(scene.model_dump(by_alias=True), sort_keys=True, separators=(",", ":")).encode("utf-8"))
    assets = candidate_dir / "assets"
    for path in sorted(assets.iterdir()) if assets.is_dir() else []:
        if path.is_file():
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _failure_feedback(report: VerifyReport | None) -> str:
    if report is None:
        return ""
    failed = [str(item.get("pred")) for item in report.keep_results if not item.get("passed")]
    frames = sorted({error.frame for error in report.layer_errors})
    return f" 이전 후보 실패 프레임={frames[:20]}, 실패 술어={failed[:20]}, 메시지={report.messages[:10]}. 이를 피한 다른 후보를 생성하세요."


def _promote_assets(source: Path, destination: Path, baseline: set[str]) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for path in (source / "assets").iterdir():
        if path.name not in baseline:
            target = destination / path.name
            if target.exists():
                raise FileExistsError(target)
            shutil.copy2(path, target)


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
    generated_kind = next(("3d" if target.property == "model" else "raster" for target in built.items if target.property in {"texture", "model"} and attachment is None), None)
    generated = generated_kind is not None
    asset_uses = 0
    seen: set[str] = set()
    baseline = {path.name for path in (sd / "assets").iterdir()} if (sd / "assets").is_dir() else set()
    attempts_run = 0
    with tempfile.TemporaryDirectory(prefix="keepframe-edit-") as temp:
        temp_root = Path(temp)
        for candidate_no in range(1, MAX_TRIES + 1):
            candidate_attachment = attachment
            if generated:
                if asset_uses >= ASSET_GEN_CAP:
                    break
                try:
                    response = AssetClient().request(
                        task="generate",
                        kind=generated_kind,
                        prompt=prompt + _failure_feedback(last_rep),
                    )
                except AssetAPIError as exc:
                    return EditResult(status="failed", summary=parsed.summary, intent=parsed, plan=built, attempts=attempts_run, error=exc.code)
                candidate_attachment = response.data
                asset_uses += 1
            candidate = temp_root / f"candidate-{candidate_no}"
            candidate.mkdir()
            if (sd / "assets").is_dir():
                shutil.copytree(sd / "assets", candidate / "assets")
            edited = apply_edit(scene, candidate, built.items, choices_map, candidate_attachment)
            digest = _candidate_digest(edited, candidate)
            if digest in seen:
                break
            seen.add(digest)
            attempts_run += 1
            html = compose(edited, candidate, candidate / "composition.html")
            probes = render(html, edited, candidate / "render")
            last_rep = verify(edited, candidate, render_result=probes, reference=scene, reference_dir=sd)
            if _passed(last_rep):
                _promote_assets(candidate, sd / "assets", baseline)
                v = new_version(root, scene_id, edited, note=parsed.summary or prompt, auto=True, parent_version=parent.id)
                return EditResult(
                    status="done",
                    summary=parsed.summary,
                    intent=parsed,
                    plan=built,
                    version=v,
                    verify=last_rep,
                    attempts=attempts_run,
                    messages=last_rep.messages,
                )
            if not generated:
                break
    return EditResult(
        status="failed",
        summary=parsed.summary,
        intent=parsed,
        plan=built,
        verify=last_rep,
        attempts=attempts_run,
        error="keep 검증을 통과하지 못했습니다.",
        messages=(last_rep.messages if last_rep else []),
    )
