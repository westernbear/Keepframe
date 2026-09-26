import json
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from keepframe.web.server import JOBS, resolve_analyze_window
from keepframe.web.workspace import create_project, load_meta, write_meta
from tests.test_web_ingest import _mp4
from tests.test_web_server import start


def _post(srv, path, payload):
    url = f"http://127.0.0.1:{srv.server_address[1]}{path}"
    req = Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req) as r:
            return r.status, json.loads(r.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


def test_posted_range_overrides_full_upload_meta():
    mode, start, end = resolve_analyze_window(
        {"mode": "full", "range": None},
        {"mode": "range", "start": 2, "end": 5},
        10,
    )
    assert (mode, start, end) == ("range", 2, 5)


def test_estimate_persists_range_on_uploaded_full_project(tmp_path):
    ws = tmp_path / "ws"
    vid = tmp_path / "a.mp4"
    _mp4(vid)
    row = create_project(ws, "Clip", vid, "full", None)
    srv = start(ws)
    try:
        code, body = _post(srv, "/api/estimate", {
            "project_id": row["id"],
            "mode": "range",
            "start": 2,
            "end": 5,
        })
    finally:
        srv.shutdown()
    assert code == 200
    assert body["scene_count"] == 1
    meta = load_meta(ws, row["id"])
    assert meta["mode"] == "range"
    assert meta["range"] == [2, 5]


def test_full_analyze_without_token_is_confirm_required(tmp_path):
    ws = tmp_path / "ws"
    vid = tmp_path / "a.mp4"
    _mp4(vid)
    row = create_project(ws, "Full", vid, "full", None)
    srv = start(ws)
    try:
        code, body = _post(srv, "/api/analyze", {"project_id": row["id"]})
    finally:
        srv.shutdown()
    assert code == 400
    assert body["error"] == "confirm required"


def _setup_token_test(tmp_path, monkeypatch):
    from keepframe.web import server as web_server
    from keepframe.jobs import JobStore

    class IdleRunner:
        def enqueue(self, job, *, spec=None, fn=None):
            pass

    monkeypatch.setattr(web_server, "JOBS", JobStore(runner=IdleRunner()))
    video = tmp_path / "a.mp4"
    _mp4(video)
    return create_project(tmp_path / "ws", "Clip", video, "full", None)


def test_full_analyze_rejects_token_for_another_project(tmp_path, monkeypatch):
    first = _setup_token_test(tmp_path, monkeypatch)
    second = create_project(tmp_path / "ws", "Other", tmp_path / "a.mp4", "full", None)
    srv = start(tmp_path / "ws")
    try:
        code, estimate_body = _post(srv, "/api/estimate", {
            "project_id": first["id"], "mode": "full", "start": 0, "end": 9,
        })
        assert code == 200
        code, body = _post(srv, "/api/analyze", {
            "project_id": second["id"], "mode": "full", "start": 0, "end": 9,
            "confirm_token": estimate_body["confirm_token"],
        })
    finally:
        srv.shutdown()
        srv.server_close()

    assert code == 400
    assert body["error"] == "confirm required"


def test_full_analyze_rejects_range_estimate_token(tmp_path, monkeypatch):
    project = _setup_token_test(tmp_path, monkeypatch)
    srv = start(tmp_path / "ws")
    try:
        code, estimate_body = _post(srv, "/api/estimate", {
            "project_id": project["id"], "mode": "range", "start": 0, "end": 2,
        })
        assert code == 200
        code, body = _post(srv, "/api/analyze", {
            "project_id": project["id"], "mode": "full", "start": 0, "end": 9,
            "confirm_token": estimate_body["confirm_token"],
        })
    finally:
        srv.shutdown()
        srv.server_close()

    assert code == 400
    assert body["error"] == "confirm required"

def test_full_analyze_rejects_token_for_different_frame_window(tmp_path, monkeypatch):
    from keepframe.web import server as web_server

    project = _setup_token_test(tmp_path, monkeypatch)
    original_probe = web_server.probe_video
    calls = 0

    def probe(path):
        nonlocal calls
        calls += 1
        info = original_probe(path)
        return {**info, "frames": info["frames"] - (calls > 1)}

    monkeypatch.setattr(web_server, "probe_video", probe)
    srv = start(tmp_path / "ws")
    try:
        code, estimate_body = _post(srv, "/api/estimate", {"project_id": project["id"], "mode": "full"})
        assert code == 200
        code, body = _post(srv, "/api/analyze", {
            "project_id": project["id"], "mode": "full",
            "confirm_token": estimate_body["confirm_token"],
        })
    finally:
        srv.shutdown()
        srv.server_close()

    assert code == 400
    assert body["error"] == "confirm required"


def test_full_analyze_accepts_matching_estimate_token(tmp_path, monkeypatch):
    project = _setup_token_test(tmp_path, monkeypatch)
    srv = start(tmp_path / "ws")
    try:
        code, estimate_body = _post(srv, "/api/estimate", {
            "project_id": project["id"], "mode": "full", "start": 0, "end": 9,
        })
        assert code == 200
        code, body = _post(srv, "/api/analyze", {
            "project_id": project["id"], "mode": "full", "start": 0, "end": 9,
            "confirm_token": estimate_body["confirm_token"],
        })
    finally:
        srv.shutdown()
        srv.server_close()

    assert code == 202
    assert body["job"]["status"] == "queued"


def test_analyze_refresh_resumes_running_job_without_token(tmp_path):
    ws = tmp_path / "ws"
    vid = tmp_path / "a.mp4"
    _mp4(vid)

    def hang():
        time.sleep(0.4)
        return {"ok": True}

    row = create_project(ws, "Full", vid, "full", None)
    job = JOBS.submit("analyze", hang, project_id=row["id"], scene_id="s1", stage="text")
    write_meta(ws, row["id"], status="analyzing", job_id=job.id)
    srv = start(ws)
    try:
        code, body = _post(srv, "/api/analyze", {"project_id": row["id"]})
    finally:
        srv.shutdown()
    assert code == 202
    assert body["job"]["id"] == job.id
    assert body["job"]["stage"] == "text"


def test_analyze_refresh_returns_finished_job_without_token(tmp_path):
    ws = tmp_path / "ws"
    vid = tmp_path / "a.mp4"
    _mp4(vid)
    row = create_project(ws, "Full", vid, "full", None)
    job = JOBS.submit("analyze", lambda: {"ok": True}, project_id=row["id"], scene_id="s1", stage="report")
    for _ in range(50):
        if JOBS.get(job.id).status == "done":
            break
        time.sleep(0.02)
    write_meta(ws, row["id"], status="review", job_id=job.id)
    srv = start(ws)
    try:
        code, body = _post(srv, "/api/analyze", {"project_id": row["id"]})
    finally:
        srv.shutdown()
    assert code == 202
    assert body["job"]["id"] == job.id
    assert body["job"]["status"] == "done"
