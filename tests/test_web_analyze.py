import json
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from keepframe.web.server import JOBS
from keepframe.web.workspace import create_project, write_meta
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


def test_analyze_refresh_resumes_running_job_without_token(tmp_path):
    ws = tmp_path / "ws"
    vid = tmp_path / "a.mp4"
    _mp4(vid)
    row = create_project(ws, "Full", vid, "full", None)

    def hang():
        time.sleep(0.4)
        return {"ok": True}

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
