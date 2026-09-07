from __future__ import annotations

from pathlib import Path
from typing import Any

from keepframe.log import get

from .spec import JobSpec

log = get("keepframe.jobs")

# Closed cloud workers import this module and call run_job(JobSpec.from_json(payload)).
# Do not reimplement analyze/render there.


def run_job(spec: JobSpec) -> dict[str, Any]:
    if spec.kind == "analyze":
        return _run_analyze(spec.args)
    if spec.kind == "render":
        return _run_render(spec.args)
    raise ValueError(f"unknown job kind {spec.kind!r}")


def _run_analyze(args: dict[str, Any]) -> dict[str, Any]:
    from keepframe.analyze.pipeline import AnalyzeOptions, analyze
    from keepframe.web.workspace import write_meta

    video = Path(args["video"])
    out_root = Path(args["out_root"])
    start, end = int(args["start"]), int(args["end"])
    workspace = Path(args["workspace"]) if args.get("workspace") else None
    project_id = args.get("project_id")
    options = None
    if args.get("options"):
        options = AnalyzeOptions(**args["options"])
    log.info("analyze start project=%s video=%s range=[%s,%s]", project_id, video, start, end)
    try:
        analyze(video, start, end, out_root, options)
        if workspace is not None and project_id:
            write_meta(workspace, project_id, status="review", job_id=None, error=None)
        log.info("analyze done project=%s", project_id)
        return {"project_id": project_id}
    except Exception as e:
        log.error("analyze failed project=%s: %s: %s", project_id, type(e).__name__, e)
        if workspace is not None and project_id:
            write_meta(workspace, project_id, status="error", error=f"{type(e).__name__}: {e}")
        raise


def _run_render(args: dict[str, Any]) -> dict[str, Any]:
    from keepframe.ir.store import load_scene
    from keepframe.render.renderer import render

    scene_path = Path(args["scene"])
    log.info("render start scene=%s out=%s mp4=%s", scene_path, args["out"], bool(args.get("mp4")))
    scene = load_scene(scene_path)
    frames = args.get("frames")
    if frames is not None:
        frames = [int(x) for x in frames]
    res = render(Path(args["html"]), scene, Path(args["out"]), frames=frames, mp4=bool(args.get("mp4")))
    log.info("render done frames=%s mp4=%s", len(res.frames), res.mp4)
    return {"frames": len(res.frames), "mp4": str(res.mp4) if res.mp4 else None}
