import json
import zipfile
from pathlib import Path

import pytest

from keepframe.ir.store import init_project
from keepframe.ir.synth import make_synthetic_scene
from keepframe.jobs import JobStore, run_job
from keepframe.render.native import prepare_native_job
from keepframe.render.plan import PlanConflict, RenderMode, approve_render_plan, create_render_plan


class RecordingRunner:
    def __init__(self):
        self.specs = []

    def enqueue(self, job, *, spec=None, fn=None):
        self.specs.append(spec)
        job.status = "queued"


class FailOnceRunner(RecordingRunner):
    def enqueue(self, job, *, spec=None, fn=None):
        self.specs.append(spec)
        if len(self.specs) == 1:
            raise RuntimeError("queue unavailable")
        job.status = "queued"


def _project(tmp_path: Path, *, approved: bool = True):
    root = tmp_path / "p1"
    scene = make_synthetic_scene(root / "scenes" / "s1", seed=8, with_text=False, frames=12)
    scene = scene.model_copy(update={"id": "s1"})
    init_project(
        root,
        {"file": "source.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, 11]},
        scene,
    )
    (root / "source.mp4").write_bytes(b"source")
    (root / "meta.json").write_text(
        json.dumps({"id": "p1", "status": "approved" if approved else "review", "version": "v1", "scene": "s1"}),
        encoding="utf-8",
    )
    return root


def _approved_plan(root: Path, mode: RenderMode):
    plan = create_render_plan(
        root,
        project_id="p1",
        scene_id="s1",
        version_id="v1",
        backend="native",
        mode=mode,
    )
    approve_render_plan(root, plan.id, digest=plan.digest, revision=0)
    return plan


def test_preview_job_uses_only_plan_scoped_snapshot_paths(tmp_path):
    root = _project(tmp_path)
    first = _approved_plan(root, "preview")
    second = _approved_plan(root, "preview")

    first_spec = prepare_native_job(root, first.id)
    second_spec = prepare_native_job(root, second.id)

    first_dir = root / "renders" / first.id / "native"
    second_dir = root / "renders" / second.id / "native"
    assert first_spec.kind == "render"
    assert Path(first_spec.args["html"]).is_relative_to(first_dir)
    assert Path(first_spec.args["scene"]).is_relative_to(first_dir)
    assert Path(first_spec.args["out"]).is_relative_to(first_dir)
    assert Path(second_spec.args["html"]).is_relative_to(second_dir)
    assert first_spec.args["html"] != second_spec.args["html"]
    assert (first_dir / "composition.html").is_file()
    assert not (root / "scenes" / "s1" / "composition.agent.html").exists()


def test_native_job_requires_an_approved_native_plan(tmp_path):
    root = _project(tmp_path)
    pending = create_render_plan(
        root, project_id="p1", scene_id="s1", version_id="v1", backend="native", mode="preview"
    )
    with pytest.raises(PlanConflict, match="approved"):
        prepare_native_job(root, pending.id)


def test_final_export_archives_snapshot_without_renders(tmp_path, monkeypatch):
    root = _project(tmp_path)
    plan = _approved_plan(root, "final")
    spec = prepare_native_job(root, plan.id)

    class Result:
        frames = [0, 1]
        mp4 = root / "renders" / plan.id / "native" / "output" / "video.mp4"

    monkeypatch.setattr("keepframe.render.renderer.render", lambda *args, **kwargs: Result())
    result = run_job(spec)

    archive = Path(result["zip"])
    assert spec.kind == "export"
    assert archive == root / "renders" / plan.id / "native" / "output" / "project.zip"
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
    assert "project.json" in names
    assert "meta.json" in names
    assert "source.mp4" in names
    assert "scenes/s1/scene.v1.json" in names
    assert "composition.html" in names
    assert not any(name == "renders" or name.startswith("renders/") for name in names)


def test_job_store_deduplicates_server_assigned_execution_id():
    runner = RecordingRunner()
    store = JobStore(runner=runner)

    first = store.submit("render", spec=None, fn=lambda: {}, project_id="p1", job_id="j-execution")
    retry = store.submit("render", spec=None, fn=lambda: {}, project_id="p1", job_id="j-execution")

    assert retry is first
    assert len(runner.specs) == 1


def test_job_store_retries_after_enqueue_failure():
    runner = FailOnceRunner()
    store = JobStore(runner=runner)

    with pytest.raises(RuntimeError, match="queue unavailable"):
        store.submit("render", fn=lambda: {}, project_id="p1", job_id="j-retry")
    job = store.submit("render", fn=lambda: {}, project_id="p1", job_id="j-retry")

    assert job.id == "j-retry"
    assert len(runner.specs) == 2


def test_worker_rejects_tampered_composition(tmp_path, monkeypatch):
    root = _project(tmp_path)
    plan = _approved_plan(root, "preview")
    spec = prepare_native_job(root, plan.id)
    Path(spec.args["html"]).write_text("tampered", encoding="utf-8")
    called = []
    monkeypatch.setattr("keepframe.render.renderer.render", lambda *args, **kwargs: called.append(True))

    with pytest.raises(PlanConflict, match="stage"):
        run_job(spec)

    assert called == []


def test_worker_rejects_unapproved_snapshot_members(tmp_path, monkeypatch):
    root = _project(tmp_path)
    plan = _approved_plan(root, "final")
    spec = prepare_native_job(root, plan.id)
    snapshot = Path(spec.args["package_root"])
    (snapshot / "renders").mkdir()
    (snapshot / "renders" / "foreign.txt").write_text("foreign", encoding="utf-8")
    monkeypatch.setattr("keepframe.render.renderer.render", lambda *args, **kwargs: None)

    with pytest.raises(PlanConflict, match="stage"):
        run_job(spec)


def test_native_stage_rejects_reparse_points(tmp_path, monkeypatch):
    root = _project(tmp_path)
    plan = _approved_plan(root, "preview")
    from keepframe.render import native

    original = getattr(native, "_reparse_point", lambda path: False)
    monkeypatch.setattr(native, "_reparse_point", lambda path: path.name == "native" or original(path), raising=False)

    with pytest.raises(PlanConflict, match="native"):
        prepare_native_job(root, plan.id)
