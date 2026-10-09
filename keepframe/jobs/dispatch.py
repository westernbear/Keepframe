from __future__ import annotations

import re
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
    if spec.kind == "export":
        return _run_export(spec.args)
    raise ValueError(f"unknown job kind {spec.kind!r}")


def _run_analyze(args: dict[str, Any]) -> dict[str, Any]:
    from keepframe.analyze.pipeline import AnalyzeOptions, analyze
    from keepframe.session.llm import vision_llm
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
        extras = {key: args[key] for key in ("scenes", "transitions", "mode") if key in args}
        project = analyze(video, start, end, out_root, options, captioner=vision_llm(workspace), **extras)
        if workspace is not None and project_id:
            first = project.scenes[0]
            write_meta(workspace, project_id, status="review", job_id=None, error=None, scene=first.id, version="v1", approved_scenes={})
        log.info("analyze done project=%s", project_id)
        return {"project_id": project_id}
    except Exception as e:
        log.error("analyze failed project=%s: %s: %s", project_id, type(e).__name__, e)
        if workspace is not None and project_id:
            write_meta(workspace, project_id, status="error", error=f"{type(e).__name__}: {e}")
        raise


def _run_render(args: dict[str, Any]) -> dict[str, Any]:
    if args.get("stage_manifest"):
        from keepframe.render.native import verify_native_stage

        verify_native_stage(args)
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


_FONT_SUFFIXES = (".ttf", ".otf", ".woff", ".woff2", ".ttc")
_FONT_COPY = re.compile(r"font-[0-9a-f]{16}\.(ttf|otf|woff2?)|\.font-.*\.tmp")


def _private_font(relative: Path) -> bool:
    """A file whose bytes never leave the server in a ZIP (R46): anything under the project's top-level `fonts/`, a
    scene copy of an uploaded face (`font-<sha16>.<ext>` and its `.font-*.tmp`) and any font file. The composition
    carries the embedded subset."""
    return (relative.parts[:1] == ("fonts",) or _FONT_COPY.fullmatch(relative.name) is not None
            or relative.name.lower().endswith(_FONT_SUFFIXES))


def project_zip(root: Path, zip_path: Path) -> None:
    """`root` as a ZIP without uploaded font bytes, symlinks or the ZIP itself."""
    import os
    import zipfile
    root, zip_path = Path(root), Path(zip_path)
    tmp = zip_path.with_name(f".{zip_path.name}.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(root):
            here = Path(dirpath)
            dirnames[:] = sorted(d for d in dirnames if not (here / d).is_symlink()
                                 and not (here == root and d == "fonts"))   # only the project's fonts/ is skipped whole
            for name in sorted(filenames):
                path = here / name
                relative = path.relative_to(root)
                if path in (zip_path, tmp) or path.is_symlink() or _private_font(relative):
                    continue
                zf.write(path, relative.as_posix())
    os.replace(tmp, zip_path)


def _run_export(args: dict[str, Any]) -> dict[str, Any]:
    if args.get("lottie_plan_id"):
        from keepframe.render.lottie import export_lottie_plan

        return export_lottie_plan(Path(args["project_root"]), args["lottie_plan_id"], args["execution_id"])
    if args.get("stage_manifest"):
        from keepframe.render.native import verify_native_stage
        from keepframe.render.plan import PlanConflict

        verify_native_stage(args)
        if not args.get("package_root"):
            raise PlanConflict("native final stage package root is missing")
    from keepframe.compose.composer import compose
    from keepframe.fonts.css import FINAL_WAIT_S
    from keepframe.fonts.registry import FontRegistry
    from keepframe.ir.store import load_scene
    from keepframe.render.renderer import render

    scene_path = Path(args["scene"])
    out = Path(args["out"])
    log.info("export start scene=%s out=%s", scene_path, out)
    scene = load_scene(scene_path)
    sd = scene_path.parent
    html_arg = args.get("html")
    html = Path(html_arg) if html_arg else sd / "composition.export.html"
    if not html_arg:   # an export is a final render: the project's fonts, waited for, never a fallback (R39)
        compose(scene, sd, html, fonts=FontRegistry.for_project(sd.parent.parent if sd.parent.name == "scenes" else None),
                font_wait=FINAL_WAIT_S)
    res = render(html, scene, out, mp4=True)
    package_root_arg = args.get("package_root")
    root = Path(package_root_arg) if package_root_arg else sd.parent.parent
    out.mkdir(parents=True, exist_ok=True)
    zip_path = out / "project.zip"
    project_zip(root, zip_path)
    log.info("export done mp4=%s zip=%s", res.mp4, zip_path)
    return {"mp4": str(res.mp4) if res.mp4 else None, "zip": str(zip_path), "frames": len(res.frames)}
