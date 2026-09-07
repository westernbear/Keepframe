from pathlib import Path

from refstudio.jobs import JobSpec, JobStore, ThreadRunner, load_runner, run_job


class RecordingRunner:
    def __init__(self) -> None:
        self.specs: list[JobSpec] = []

    def enqueue(self, job, *, spec=None, fn=None) -> None:
        assert spec is not None
        self.specs.append(spec)
        job.status = "queued"


def test_run_job_analyze_calls_pipeline(tmp_path, monkeypatch):
    called = {}

    def fake_analyze(video, start, end, out_root, options=None):
        called["args"] = (Path(video), start, end, Path(out_root), options)

    monkeypatch.setattr("refstudio.analyze.pipeline.analyze", fake_analyze)
    spec = JobSpec(
        kind="analyze",
        args={"video": "/tmp/a.mp4", "start": 0, "end": 11, "out_root": str(tmp_path)},
    )
    assert run_job(spec) == {"project_id": None}
    assert called["args"][:4] == (Path("/tmp/a.mp4"), 0, 11, tmp_path)


def test_run_job_render_calls_renderer(tmp_path, monkeypatch):
    called = {}

    class FakeRes:
        frames = [0, 1]
        mp4 = tmp_path / "out.mp4"

    def fake_load(path):
        called["scene"] = Path(path)
        return object()

    def fake_render(html, scene, out, frames=None, mp4=False):
        called["html"] = Path(html)
        called["out"] = Path(out)
        called["mp4"] = mp4
        return FakeRes()

    monkeypatch.setattr("refstudio.ir.store.load_scene", fake_load)
    monkeypatch.setattr("refstudio.render.renderer.render", fake_render)
    spec = JobSpec(
        kind="render",
        args={"scene": "/tmp/scene.json", "html": "/tmp/c.html", "out": str(tmp_path / "r"), "mp4": True},
    )
    assert run_job(spec) == {"frames": 2, "mp4": str(tmp_path / "out.mp4")}
    assert called["scene"] == Path("/tmp/scene.json")
    assert called["mp4"] is True


def test_jobstore_swaps_runner_without_running_fn():
    runner = RecordingRunner()
    store = JobStore(runner=runner)
    spec = JobSpec(kind="analyze", args={"video": "v.mp4", "start": 0, "end": 1, "out_root": "out"})
    j = store.submit("analyze", spec=spec, project_id="p1")
    assert j.status == "queued"
    assert runner.specs[0].to_json() == spec.to_json()


def test_load_runner_default_is_thread():
    assert isinstance(load_runner("thread"), ThreadRunner)


def test_jobspec_roundtrip():
    spec = JobSpec(kind="render", args={"scene": "s.json", "html": "c.html", "out": "r"})
    assert JobSpec.from_json(spec.to_json()) == spec
