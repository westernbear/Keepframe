"""PYTEST_DONT_REWRITE: do not expose pairing credentials in assertion output."""

import http.client
import hashlib
import errno
import json
import logging
import queue
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlparse

import pytest

from tests.test_web_server import start
from tests.test_ae_spec import png
from keepframe.ir.schema import Background, Canonical, Element, FontGuess, Scene
from keepframe.ir.store import init_project, new_version, scene_dir
from keepframe.ae.spec import comp_spec


INFO = {"ae_version": "25.0", "extension_version": "1.0.0", "os": "Windows", "fonts": []}
EXTENSION = {"X-Keepframe-Extension": "1.0.0", "Host": "keepframe.tailnet.ts.net:443"}


@pytest.fixture
def server(tmp_path):
    srv = start(tmp_path)
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "p1"
    directory = scene_dir(root, "s1")
    png(directory / "assets" / "image.png", 20, 12)
    (directory / "private.txt").write_text("never serve this")
    value = Scene(id="s1", size=(320, 180), fps=30, frames=60, background=Background(value="#fff"),
                  elements=[Element(id="e1", kind="sprite", visible=(0, 59),
                                    canonical=Canonical(width=10, height=6, texture="assets/image.png")),
                            Element(id="title", kind="text", visible=(0, 59),
                                    canonical=Canonical(width=100, height=32, text="Hello",
                                                        font=FontGuess(family_guess="Example")))])
    init_project(root, {"file": "source.mp4"}, value)
    return value


def request(server, method, path, data=None, *, headers=None, body=None, browser=False):
    headers = dict(headers or {})
    if browser:
        headers.setdefault("Origin", f"http://127.0.0.1:{server.server_address[1]}")
    if data is not None:
        body = json.dumps(data).encode()
        headers.setdefault("Content-Type", "application/json")
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        payload = response.read()
        return response.status, dict(response.getheaders()), payload
    finally:
        conn.close()


def json_request(*args, **kwargs):
    status, _, body = request(*args, **kwargs)
    return status, json.loads(body) if body else None


def pair(server):
    status, code = json_request(server, "POST", "/api/ae/codes", browser=True)
    assert status == 200
    status, paired = json_request(server, "POST", "/api/ae/pair",
                                  {"code": code["code"], "info": INFO}, headers=EXTENSION)
    assert status == 200
    assert set(paired) == {"device_id", "token", "server_name"}
    assert paired["server_name"] == socket.gethostname()
    return paired["device_id"], EXTENSION | {"Authorization": "Bearer " + paired["token"]}


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/ae/codes"),
    ("DELETE", "/api/ae/devices/missing"), ("POST", "/api/ae/send"), ("POST", "/api/ae/verify"),
])
def test_browser_routes_reject_cross_origin_first(server, method, path):
    status, error = json_request(server, method, path, headers={"Origin": "https://evil.example"})
    assert status == 403 and isinstance(error["error"], str)


@pytest.mark.parametrize("path", ["/api/ae/devices", "/api/ae/state?project=p1&scene=s1"])
def test_browser_get_routes_require_allowed_host_only(server, project, path):
    port = server.server_address[1]
    for headers, expected in (
        ({"Host": f"localhost:{port}"}, 200),
        ({"Host": f"localhost:{port}", "Origin": "https://evil.example"}, 200),
        ({"Host": f"evil.example:{port}"}, 403),
        ({"Host": "localhost:1"}, 403),
        ({"Host": "localhost:0"}, 403),
        ({"Host": "localhost"}, 403),
    ):
        status, response_headers, body = request(server, "GET", path, headers=headers)
        assert status == expected
        assert not any(key.lower().startswith("access-control-") for key in response_headers)
        if expected == 403:
            assert json.loads(body) == {"error": "browser origin is not allowed"}
    for hosts in ([], [f"localhost:{port}", f"localhost:{port}"]):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.putrequest("GET", path, skip_host=True)
            for host in hosts:
                conn.putheader("Host", host)
            conn.endheaders()
            response = conn.getresponse()
            assert response.status == 403
            assert json.loads(response.read()) == {"error": "browser origin is not allowed"}
        finally:
            conn.close()


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/ae/codes"), ("POST", "/api/ae/send"), ("POST", "/api/ae/verify"),
    ("DELETE", "/api/ae/devices/missing"),
])
def test_browser_mutations_require_origin(server, method, path):
    status, error = json_request(server, method, path, data={})
    assert status == 403 and error == {"error": "browser origin is not allowed"}


def test_pairing_and_same_origin_device_routes(server):
    device, headers = pair(server)
    status, listed = json_request(server, "GET", "/api/ae/devices", browser=True)
    assert status == 200 and listed["devices"][0]["id"] == device
    assert listed["devices"][0]["connected"] is False
    assert listed["devices"][0]["host_build"] is listed["devices"][0]["panel_build"] is None
    status, _ = json_request(server, "POST", "/api/ae/info", {"info": INFO | {"ae_version": "26.0"}},
                             headers=headers)
    assert status == 204
    status, listed = json_request(server, "GET", "/api/ae/devices", browser=True)
    assert listed["devices"][0]["ae_version"] == "26.0"
    assert json_request(server, "DELETE", f"/api/ae/devices/{device}", browser=True)[0] == 204
    status, error = json_request(server, "POST", "/api/ae/info", {"info": INFO}, headers=headers)
    assert status == 401
    assert error == {"error": "device is not paired; pair again from the Keepframe web page"}


def test_pair_and_info_return_build_ids_in_device_rows(server):
    builds = {"host_build": "123-abc1234", "panel_build": "124-def5678-dirty"}
    _, code = json_request(server, "POST", "/api/ae/codes", browser=True)
    status, paired = json_request(server, "POST", "/api/ae/pair", {"code": code["code"], "info": INFO | builds},
                                  headers=EXTENSION)
    assert status == 200
    headers = EXTENSION | {"Authorization": "Bearer " + paired["token"]}
    status, listed = json_request(server, "GET", "/api/ae/devices", browser=True)
    assert status == 200
    assert all(listed["devices"][0][key] == value for key, value in builds.items())
    builds = {"host_build": "dev", "panel_build": "dev"}
    assert json_request(server, "POST", "/api/ae/info", {"info": INFO | builds}, headers=headers)[0] == 204
    _, listed = json_request(server, "GET", "/api/ae/devices", browser=True)
    assert all(listed["devices"][0][key] == value for key, value in builds.items())
    for field in builds:
        assert json_request(server, "POST", "/api/ae/info", {"info": INFO | {field: "x" * 65}}, headers=headers) == (
            400, {"error": f"invalid {field}"})
    assert json_request(server, "POST", "/api/ae/info", {"info": INFO}, headers=headers)[0] == 204
    _, listed = json_request(server, "GET", "/api/ae/devices", browser=True)
    assert listed["devices"][0]["host_build"] is listed["devices"][0]["panel_build"] is None


@pytest.mark.parametrize("version", [None, "0.9.0", "2.0.0", "bad"])
def test_pair_requires_compatible_extension(server, version):
    headers = {"Host": "keepframe.tailnet.ts.net:443"}
    if version is not None:
        headers["X-Keepframe-Extension"] = version
    status, error = json_request(server, "POST", "/api/ae/pair", {"code": "wrong", "info": INFO},
                                 headers=headers)
    assert status == 426
    assert error == {"error": "update the Keepframe extension", "download": "/ae/keepframe.zxp"}


def test_valid_semver_and_invalid_json_constants(server):
    status, error = json_request(server, "POST", "/api/ae/pair", {"code": "wrong", "info": INFO},
                                 headers=EXTENSION | {"X-Keepframe-Extension": "1.2.3-beta.1+build.5"})
    assert status == 401
    for version in ("01.0.0", "1.0.0-bad_name", "1.0.0-01", "1.0.0-a..b", "9" * 5000 + ".0.0"):
        assert json_request(server, "POST", "/api/ae/pair", {"code": "wrong", "info": INFO},
                            headers=EXTENSION | {"X-Keepframe-Extension": version})[0] == 426
    for body in (b'{"code": NaN}', b'{"code": Infinity}', b'{"code": -Infinity}', b"[" * 2000):
        status, error = json_request(server, "POST", "/api/ae/pair", body=body, headers=EXTENSION)
        assert status == 400 and error == {"error": "bad json"}


def test_pair_bad_code_invalid_info_and_json(server):
    for code in ("wrong", "\ud800"):
        status, error = json_request(server, "POST", "/api/ae/pair", {"code": code, "info": INFO},
                                     headers=EXTENSION)
        assert status == 401 and error["error"] == "pairing code is invalid or expired"
    _, code = json_request(server, "POST", "/api/ae/codes", browser=True)
    status, error = json_request(server, "POST", "/api/ae/pair", {"code": code["code"], "info": {}},
                                 headers=EXTENSION)
    assert status == 400 and error["error"] == "invalid ae_version"
    for body in (b"{broken", b"[]", b"", b" " * 70000 + b"{broken"):
        status, error = json_request(server, "POST", "/api/ae/pair", body=body, headers=EXTENSION)
        assert status == 400 and error == {"error": "bad json"}
    status, error = json_request(server, "POST", "/api/ae/pair", body=b"",
                                 headers=EXTENSION | {"Content-Length": str(8 * 1024 * 1024 + 1)})
    assert status == 413 and isinstance(error["error"], str)


def test_extension_requires_bearer(server):
    for headers in (EXTENSION, EXTENSION | {"Authorization": "Bearer wrong"}):
        status, error = json_request(server, "POST", "/api/ae/info", {"info": INFO}, headers=headers)
        assert status == 401
        assert error == {"error": "device is not paired; pair again from the Keepframe web page"}


def poll(server, headers, wait=0):
    return json_request(server, "GET", f"/api/ae/next?wait={wait}", headers=headers)


def send(server, **changes):
    return json_request(server, "POST", "/api/ae/send", {"project": "p1", "scene": "s1", **changes},
                        browser=True)


def state(server):
    status, value = json_request(server, "GET", "/api/ae/state?project=p1&scene=s1", browser=True)
    assert status == 200
    return value


def test_send_uses_current_version_and_most_recent_connected_device(server, project, tmp_path):
    first, first_headers = pair(server)
    second, second_headers = pair(server)
    assert poll(server, first_headers)[0] == 204
    assert poll(server, second_headers)[0] == 204
    new_version(tmp_path / "p1", "s1", project, "edited")
    status, value = send(server, force=True)
    assert status == 202
    job = value["job"]
    assert (job["device"], job["project"], job["scene"], job["version"], job["kind"], job["params"]) == (
        second, "p1", "s1", "v2", "sync", {"force": True})
    status, value = send(server, device=first, version="v1")
    assert status == 202 and value["job"]["device"] == first and value["job"]["version"] == "v1"
    assert value["job"]["params"] == {"force": False}
    snapshot = state(server)
    assert set(snapshot) == {"jobs", "last_synced", "devices", "progress", "verify"}
    assert snapshot["verify"] is None
    assert len(snapshot["jobs"]) == 2 and snapshot["last_synced"] == snapshot["progress"] == {}


def test_send_with_null_device_auto_picks(server, project):
    assert send(server, device=None) == (409, {"error": "no connected After Effects"})
    device, headers = pair(server)
    assert poll(server, headers)[0] == 204
    status, value = send(server, device=None)
    assert status == 202 and value["job"]["device"] == device


def test_send_without_connected_device_and_unknown_identities(server, project):
    assert send(server) == (409, {"error": "no connected After Effects"})
    device, _ = pair(server)
    assert send(server)[0] == 409
    assert send(server, device=device)[0] == 202  # Explicit paired devices need not be connected.
    for changes in ({"project": "missing"}, {"scene": "missing"}, {"version": "missing"},
                    {"project": "../p1"}, {"scene": "../s1"}, {"version": "../v1"},
                    {"project": []}, {"scene": None}, {"device": "missing"}):
        status, error = send(server, **changes)
        assert status == 404 and isinstance(error["error"], str)
    assert json_request(server, "DELETE", f"/api/ae/devices/{device}", browser=True)[0] == 204
    assert send(server, device=device)[0] == 404


def test_scene_and_upload_paths_cannot_escape_project(server, project, tmp_path):
    root = tmp_path / "p1"
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "alias").symlink_to(root, target_is_directory=True)
    assert send(server, project="alias")[0] == 404
    # Even a manifest version cannot point through a scene-file symlink outside the project.
    scene_file = scene_dir(root, "s1") / "scene.v1.json"
    outside_scene = outside / "scene.json"
    outside_scene.write_bytes(scene_file.read_bytes())
    scene_file.unlink()
    scene_file.symlink_to(outside_scene)
    assert send(server)[0] == 404
    assert send(server, version="v1")[0] == 404
    scene_file.unlink()
    scene_file.write_bytes(outside_scene.read_bytes())
    headers, job = running_upload_job(server, "package")
    (root / "ae").symlink_to(outside, target_is_directory=True)
    status, error = json_request(server, "PUT", f'/api/ae/jobs/{job["id"]}/files/project.zip',
                                 headers=headers, body=b"no")
    assert status == 404 and isinstance(error["error"], str)
    assert not (outside / "s1").exists()


def test_long_poll_times_out_and_wakes_immediately_when_browser_sends(server, project):
    device, headers = pair(server)
    started = time.monotonic()
    assert poll(server, headers, 0.2) == (204, None)
    assert 0.18 <= time.monotonic() - started < 1
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(poll, server, headers, 2)
        time.sleep(0.1)
        assert not future.done()
        started = time.monotonic()
        status, sent = send(server)
        status_poll, value = future.result(timeout=1)
        assert time.monotonic() - started < 0.5
    assert status == 202 and status_poll == 200
    assert value["job"]["id"] == sent["job"]["id"] and value["job"]["state"] == "running"
    assert value["job"]["device"] == device


@pytest.mark.parametrize("has_next", [False, True])
def test_idle_poll_abandons_running_sync_and_continues(server, project, has_next):
    device, headers = pair(server)
    routes = server.ae_routes
    dependent = routes.jobs.enqueue(device, "render_final", "p1", "s1", "v1")
    status, value = poll(server, headers)
    assert status == 200 and value["job"]["id"] == dependent.depends_on
    running = value["job"]
    assert json_request(server, "POST", f'/api/ae/jobs/{running["id"]}/progress',
                        {"stage": "sync", "done": 1, "total": 2}, headers=headers)[0] == 204
    queued = routes.jobs.enqueue(device, "sync", "p1", "s1", "v1") if has_next else None
    assert json_request(server, "POST", "/api/ae/info", {"info": INFO}, headers=headers)[0] == 204
    assert poll(server, headers, "bad")[0] == 400
    assert routes.jobs.get(running["id"]).state == "running"
    status, value = poll(server, headers)
    reason = "the Keepframe panel lost this job (it asked for new work); send again"
    failed = routes.jobs.get(running["id"])
    assert failed.state == "failed" and failed.error == reason and failed.finished is not None
    assert routes.jobs.get(dependent.id).state == "failed"
    assert routes.jobs.get(dependent.id).error == "sync failed: " + reason
    assert running["id"] not in routes._progress
    if has_next:
        assert status == 200 and value["job"]["id"] == queued.id and value["job"]["state"] == "running"
    else:
        assert (status, value) == (204, None)


@pytest.mark.parametrize("exception", [ConnectionResetError, BrokenPipeError, ConnectionAbortedError])
@pytest.mark.parametrize("path,status", [
    ("/api/ae/next?wait=0.01", 204), ("/api/ae/next?wait=0.01", 200),
    ("/api/ae/next?wait=bad", 400), ("/api/ae/jobs/missing/spec", 404),
])
def test_response_disconnect_logs_info_without_retry_or_escape(server, monkeypatch, caplog, exception, path, status):
    device, headers = pair(server)
    if status == 200:
        server.ae_routes.jobs.enqueue(device, "sync", "p1", "s1", "v1")
    handler = object.__new__(server.RequestHandlerClass)
    handler.server = server
    handler.client_address = ("127.0.0.1", 0)
    handler.command, handler.path, handler.request_version = "GET", path, "HTTP/1.1"
    handler.requestline = f"GET {path} HTTP/1.1"
    handler.headers = headers
    responses = []
    send_response = handler.send_response

    def record_response(code, *args):
        responses.append(code)
        return send_response(code, *args)

    class ClosedConnection:
        def write(self, body):
            if status == 200 and body.startswith(b"HTTP/"):
                return len(body)
            raise exception("client closed")

    handler.wfile = ClosedConnection()
    monkeypatch.setattr(handler, "send_response", record_response)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert server.ae_routes.handle_get(handler, urlparse(path)) is True
    assert responses == [status]
    assert not any(record.levelno >= logging.ERROR or record.exc_info for record in caplog.records)
    records = [record for record in caplog.records if record.name == "keepframe.ae"]
    assert len(records) == (2 if status in (400, 404) else 1)
    if status in (400, 404):
        assert records[0].levelno == logging.WARNING
        assert urlparse(path).path in records[0].getMessage()
    assert records[-1].levelno == logging.INFO
    assert records[-1].getMessage() == f"client closed the connection during GET {urlparse(path).path}"
    assert headers["Authorization"] not in caplog.text
    assert headers["Authorization"][7:] not in caplog.text
    assert headers["Host"] not in caplog.text


def test_next_updates_status_and_rejects_invalid_wait_or_status(server):
    device, headers = pair(server)
    name = "한글 Project.aep"
    assert poll(server, headers | {"X-Keepframe-Project": quote(name), "X-Keepframe-Project-Saved": "1"})[0] == 204
    _, listed = json_request(server, "GET", "/api/ae/devices", browser=True)
    row = listed["devices"][0]
    assert row["id"] == device and row["connected"] is True
    assert row["project_name"] == name and row["project_saved"] is True
    for wait in ("-1", "31", "nan", "inf", "bad"):
        status, error = poll(server, headers, wait)
        assert status == 400 and isinstance(error["error"], str)
    for changes in ({"X-Keepframe-Project": "x" * 257}, {"X-Keepframe-Project-Saved": "true"}):
        assert poll(server, headers | changes)[0] == 400


def test_extension_header_and_bearer_required_for_every_route(server):
    _, headers = pair(server)
    routes = [("GET", "/api/ae/next?wait=0"), ("GET", "/api/ae/jobs/missing/spec"),
              ("GET", "/api/ae/jobs/missing/assets/e1.png"), ("PUT", "/api/ae/jobs/missing/files/final.mp4"),
              ("POST", "/api/ae/jobs/missing/result"), ("POST", "/api/ae/jobs/missing/progress"),
              ("POST", "/api/ae/info")]
    for method, path in routes:
        assert json_request(server, method, path, headers=EXTENSION)[0] == 401
        assert json_request(server, method, path, headers={"Authorization": headers["Authorization"]})[0] == 426


def test_zxp_download_missing_and_present(server, tmp_path, monkeypatch):
    from keepframe.ae import api
    directory = tmp_path / "static"
    directory.mkdir()
    monkeypatch.setattr(api, "STATIC", directory)
    status, error = json_request(server, "GET", "/ae/keepframe.zxp")
    assert status == 404 and error == {"error": "extension not built; run scripts/build_zxp.sh"}
    (directory / "keepframe.zxp").write_bytes(b"dummy zip")
    status, headers, body = request(server, "GET", "/ae/keepframe.zxp", headers={"Host": "public.example:443"})
    assert status == 200 and body == b"dummy zip"
    assert headers["content-type"] == "application/zip"
    assert headers["content-disposition"] == 'attachment; filename="keepframe.zxp"'
    assert int(headers["content-length"]) == len(body)


def test_sweeper_stops_when_server_is_closed(tmp_path):
    server = start(tmp_path)
    sweeper = server.ae_routes._sweeper
    assert sweeper.daemon and sweeper.is_alive()
    server.shutdown()
    server.server_close()
    assert not sweeper.is_alive()


def running_sync(server):
    device, headers = pair(server)
    assert poll(server, headers)[0] == 204
    status, sent = send(server)
    assert status == 202
    status, value = poll(server, headers)
    assert status == 200 and value["job"]["id"] == sent["job"]["id"]
    return device, headers, value["job"]


def finish(server, headers, job, **data):
    return json_request(server, "POST", f'/api/ae/jobs/{job["id"]}/result', data, headers=headers)


def running_upload_job(server, kind):
    device, headers, synced = running_sync(server)
    assert finish(server, headers, synced, ok=True, result={"applied": True})[0] == 200
    queued = server.ae_routes.jobs.enqueue(device, kind, "p1", "s1", "v1")
    status, value = poll(server, headers)
    assert status == 200 and value["job"]["id"] == queued.id
    return headers, value["job"]


def test_spec_and_assets_use_job_version_and_device_fonts(server, project, tmp_path):
    _, headers, job = running_sync(server)
    fonts = [{"family": "Example", "style": None, "postscript": None}]
    assert json_request(server, "POST", "/api/ae/info", {"info": INFO | {"fonts": fonts}}, headers=headers)[0] == 204
    edited = project.model_copy(deep=True)
    edited.size = (640, 360)
    new_version(tmp_path / "p1", "s1", edited, "new size")
    prefix = f'/api/ae/jobs/{job["id"]}'
    status, response_headers, body = request(server, "GET", prefix + "/spec", headers=headers)
    assert status == 200 and response_headers["content-type"] == "application/json"
    expected = comp_spec(project, scene_dir(tmp_path / "p1", "s1"), project="p1", scene_id="s1", version="v1", fonts=fonts)
    assert json.loads(body) == expected
    status, response_headers, body = request(server, "GET", prefix + "/assets/e1.png", headers=headers)
    assert status == 200 and body == (scene_dir(tmp_path / "p1", "s1") / "assets" / "image.png").read_bytes()
    assert response_headers["content-type"] == "image/png"
    assert int(response_headers["content-length"]) == len(body)
    assert response_headers["X-Keepframe-Sha256"] == hashlib.sha256(body).hexdigest()


def test_asset_outside_spec_is_refused(server, project):
    _, headers, job = running_sync(server)
    prefix = f'/api/ae/jobs/{job["id"]}/assets/'
    for name in ("private.txt", "image.png", "assets/image.png", "../private.txt", "%2e%2e%2fprivate.txt",
                 "%2Ftmp%2Fsecret", "/tmp/secret", "e1.png/extra", "e1%5cpng", "e1..png"):
        status, error = json_request(server, "GET", prefix + name, headers=headers)
        assert status == 404 and isinstance(error["error"], str)


def test_job_routes_hide_other_devices_jobs(server, project):
    _, _, job = running_sync(server)
    _, stranger = pair(server)
    prefix = f'/api/ae/jobs/{job["id"]}'
    for method, suffix in (("GET", "/spec"), ("GET", "/assets/e1.png"), ("PUT", "/files/final.mp4"),
                           ("POST", "/result"), ("POST", "/progress")):
        status, error = json_request(server, method, prefix + suffix, {"ok": True}, headers=stranger)
        assert status == 404 and isinstance(error["error"], str)


def test_result_progress_state_and_non_running_conflict(server, project):
    _, headers, job = running_sync(server)
    prefix = f'/api/ae/jobs/{job["id"]}'
    progress = {"stage": "sync", "done": 1, "total": 2}
    assert json_request(server, "POST", prefix + "/progress", progress, headers=headers) == (204, None)
    assert state(server)["progress"] == {job["id"]: progress}
    status, value = finish(server, headers, job, ok=True, result={"applied": True})
    assert status == 200 and value["job"]["state"] == "done"
    snapshot = state(server)
    assert snapshot["progress"] == {} and snapshot["last_synced"] == {job["device"]: "v1"}
    assert finish(server, headers, job, ok=True)[0] == 409
    assert json_request(server, "POST", prefix + "/progress", progress, headers=headers)[0] == 409
    assert send(server)[0] == 202
    _, value = poll(server, headers)
    status, value = finish(server, headers, value["job"], ok=False, error="AE failed", line=42)
    assert status == 200 and value["job"]["state"] == "failed"
    assert value["job"]["error"] == "AE failed (line 42)"


def test_result_body_cap_invalid_json_and_invalid_progress(server, project):
    _, headers, job = running_sync(server)
    prefix = f'/api/ae/jobs/{job["id"]}'
    assert json_request(server, "POST", prefix + "/result", body=b"", headers=headers | {
        "Content-Length": str(1024 * 1024 + 1)})[0] == 413
    for value in ({}, {"stage": "sync", "done": -1, "total": 2}, {"stage": "sync", "done": 3, "total": 2},
                  {"stage": [], "done": 1, "total": 2}):
        status, error = json_request(server, "POST", prefix + "/progress", value, headers=headers)
        assert status == 400 and isinstance(error["error"], str)


@pytest.mark.parametrize("data", [{"ok": True, "result": {"applied": True}, "line": None},
                                  {"ok": False, "error": "AE failed", "line": None},
                                  {"ok": False, "error": "x" * 2000, "line": 42}])
def test_result_null_line_and_composed_error_limit(server, project, data):
    _, headers, job = running_sync(server)
    status, value = finish(server, headers, job, **data)
    assert status == 200
    expected = data.get("error")
    if data["line"] is not None:
        expected = f'{expected} (line {data["line"]})'[:2000]
    assert value["job"]["error"] == expected
    assert value["job"]["state"] == ("done" if data["ok"] else "failed")


@pytest.mark.parametrize("data,reason", [
    ({"ok": "yes"}, "invalid ok: expected a boolean"),
    ({"ok": True, "result": []}, "invalid result: expected a JSON dictionary"),
    ({"ok": False, "error": ""}, "invalid error: required, at most 2000 characters"),
    ({"ok": False, "error": "x" * 2001}, "invalid error: required, at most 2000 characters"),
    ({"ok": False, "error": "AE failed", "line": "42"}, "invalid error or line"),
    (None, "bad json"),
])
def test_invalid_result_finishes_job_and_frees_device(server, project, data, reason):
    device, headers, job = running_sync(server)
    prefix = f'/api/ae/jobs/{job["id"]}'
    assert json_request(server, "POST", prefix + "/progress", {"stage": "sync", "done": 1, "total": 2},
                        headers=headers)[0] == 204
    queued = server.ae_routes.jobs.enqueue(device, "sync", "p1", "s1", "v1")
    if data is None:
        status, error = json_request(server, "POST", prefix + "/result", body=b"{", headers=headers)
    else:
        status, error = finish(server, headers, job, **data)
    assert status == 400 and error == {"error": reason}
    failed = server.ae_routes.jobs.get(job["id"])
    assert failed.state == "failed" and failed.error == "invalid result from the extension: " + reason
    assert job["id"] not in server.ae_routes._progress
    status, value = poll(server, headers)
    assert status == 200 and value["job"]["id"] == queued.id


@pytest.mark.parametrize("kind,name,cap", [("render_frames", "frame_0001.png", 25 * 1024 * 1024),
                                         ("render_final", "final.mp4", 4 * 1024**3),
                                         ("package", "project.zip", 4 * 1024**3)])
def test_upload_allowlist_length_caps_and_success(server, project, tmp_path, kind, name, cap):
    headers, job = running_upload_job(server, kind)
    prefix = f'/api/ae/jobs/{job["id"]}/files/'
    for bad in ("other.mp4", "../" + name, "%2e%2e%2f" + name, name + "/extra", "frame_001.png"):
        status, error = json_request(server, "PUT", prefix + bad, headers=headers, body=b"no")
        assert status == 404 and isinstance(error["error"], str)
    # http.client adds Content-Length: 0 for PUT; use putrequest to omit it.
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    conn.putrequest("PUT", prefix + name, skip_host=True)
    for key, value in headers.items():
        conn.putheader(key, value)
    conn.endheaders()
    response = conn.getresponse()
    assert response.status == 411 and isinstance(json.loads(response.read())["error"], str)
    conn.close()
    assert json_request(server, "PUT", prefix + name, headers=headers | {"Content-Length": str(cap + 1)}, body=b"")[0] == 413
    data = b"uploaded content\0" * 100000  # More than one streaming chunk.
    status, _ = json_request(server, "PUT", prefix + name, headers=headers, body=data)
    assert status == 204
    directory = tmp_path / "p1" / "ae" / "s1" / "v1"
    assert (directory / name).read_bytes() == data
    assert not list(directory.glob(".*.part-*"))
    assert finish(server, headers, job, ok=kind != "render_frames", error="upload-only test complete")[0] == 200
    assert job["id"] not in server.ae_routes._uploaded
    # A completed job must reject before reading; avoid racing a large send with the early response.
    assert json_request(server, "PUT", prefix + name, headers=headers | {
        "Content-Length": str(len(data))}, body=b"")[0] == 409
    assert json_request(server, "GET", f'/api/ae/jobs/{job["id"]}/spec', headers=headers)[0] == 404


def test_sync_jobs_cannot_upload_and_frame_jobs_have_sixteen_file_cap(server, project):
    _, headers, sync = running_sync(server)
    assert json_request(server, "PUT", f'/api/ae/jobs/{sync["id"]}/files/frame_0001.png',
                        headers=headers, body=b"no")[0] == 404
    assert finish(server, headers, sync, ok=True, result={"applied": True})[0] == 200
    queued = server.ae_routes.jobs.enqueue(sync["device"], "render_frames", "p1", "s1", "v1")
    assert poll(server, headers)[0] == 200
    prefix = f"/api/ae/jobs/{queued.id}/files/"
    for index in range(16):
        assert json_request(server, "PUT", prefix + f"frame_{index:04d}.png", headers=headers, body=b"png")[0] == 204
    assert json_request(server, "PUT", prefix + "frame_0000.png", headers=headers, body=b"replace")[0] == 204
    status, error = json_request(server, "PUT", prefix + "frame_0016.png", headers=headers, body=b"png")
    assert status == 413 and isinstance(error["error"], str)


def test_partial_upload_is_discarded(server, project, tmp_path):
    headers, job = running_upload_job(server, "render_frames")
    directory = tmp_path / "p1" / "ae" / "s1" / "v1"
    path = f'/api/ae/jobs/{job["id"]}/files/frame_0001.png'
    with socket.create_connection(server.server_address, timeout=5) as conn:
        raw_headers = "\r\n".join(f"{key}: {value}" for key, value in headers.items())
        conn.sendall((f"PUT {path} HTTP/1.1\r\n{raw_headers}\r\nContent-Length: 100\r\n\r\n").encode() + b"short")
        conn.shutdown(socket.SHUT_WR)
        response = b""
        while chunk := conn.recv(4096):
            response += chunk
    assert response.startswith(b"HTTP/1.0 400")
    assert b'"error"' in response
    assert directory.exists() and list(directory.iterdir()) == []
    # Fully closing the client also discards the temporary file, even if no response can be delivered.
    with socket.create_connection(server.server_address, timeout=5) as conn:
        conn.sendall((f"PUT {path} HTTP/1.1\r\n{raw_headers}\r\nContent-Length: 100\r\n\r\n").encode() + b"short")
        deadline = time.monotonic() + 2
        while not list(directory.glob(".*.part-*")) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert list(directory.glob(".*.part-*"))
    deadline = time.monotonic() + 2
    while list(directory.iterdir()) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert list(directory.iterdir()) == []
    # The same name can be retried after interruption.
    assert json_request(server, "PUT", path, headers=headers, body=b"complete")[0] == 204


@pytest.mark.parametrize("code,status", [(errno.ENOSPC, 507), (errno.EACCES, 500), (None, 500)])
def test_upload_replace_failure_discards_temp_and_keeps_existing_file(server, project, tmp_path, monkeypatch, caplog, code, status):
    from keepframe.ae import api
    headers, job = running_upload_job(server, "package")
    path = f'/api/ae/jobs/{job["id"]}/files/project.zip'
    assert json_request(server, "PUT", path, headers=headers, body=b"original")[0] == 204
    original = api.os.replace

    def fail_upload(source, destination):
        if ".part-" in str(source):
            raise OSError(code, "simulated disk error")
        original(source, destination)

    monkeypatch.setattr(api.os, "replace", fail_upload)
    response_status, error = json_request(server, "PUT", path, headers=headers, body=b"replacement")
    assert response_status == status and "simulated disk error" in error["error"]
    directory = tmp_path / "p1" / "ae" / "s1" / "v1"
    assert (directory / "project.zip").read_bytes() == b"original"
    assert list(directory.iterdir()) == [directory / "project.zip"]
    assert any(record.name == "keepframe.ae" and record.exc_info for record in caplog.records)
    assert headers["Authorization"] not in caplog.text
    assert headers["Host"] not in caplog.text


def test_assets_are_also_available_to_the_owning_render_job(server, project):
    headers, job = running_upload_job(server, "render_frames")
    status, _, body = request(server, "GET", f'/api/ae/jobs/{job["id"]}/assets/e1.png', headers=headers)
    assert status == 200 and body.startswith(b"\x89PNG")


def test_request_logs_do_not_include_credentials(server, caplog):
    device, headers = pair(server)
    assert poll(server, headers)[0] == 204
    assert headers["Authorization"] not in caplog.text
    assert headers["Authorization"][7:] not in caplog.text
    assert json_request(server, "DELETE", f"/api/ae/devices/{device}", browser=True)[0] == 204


def start_stalled_upload(server, headers, path, directory):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=2)
    conn.request("PUT", path, body=b"short", headers=headers | {"Content-Length": "100"})
    deadline = time.monotonic() + 1
    while not list(directory.glob(".*.part-*")) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert list(directory.glob(".*.part-*"))
    return conn


def test_stalled_upload_does_not_block_other_upload_and_discards_temp(server, project, tmp_path, monkeypatch):
    from keepframe.ae import api
    monkeypatch.setattr(api, "UPLOAD_IDLE_TIMEOUT", 0.3, raising=False)
    original_setup = server.RequestHandlerClass.setup
    original_finish = server.RequestHandlerClass.finish
    restored = queue.Queue()

    def setup(handler):
        original_setup(handler)
        handler.connection.settimeout(1.5)

    def finished(handler):
        if getattr(handler, "command", None) == "PUT":
            restored.put(handler.connection.gettimeout())
        original_finish(handler)

    monkeypatch.setattr(server.RequestHandlerClass, "setup", setup)
    monkeypatch.setattr(server.RequestHandlerClass, "finish", finished)
    headers, job = running_upload_job(server, "render_frames")
    directory = tmp_path / "p1" / "ae" / "s1" / "v1"
    prefix = f'/api/ae/jobs/{job["id"]}/files/'
    conn = start_stalled_upload(server, headers, prefix + "frame_0001.png", directory)
    try:
        started = time.monotonic()
        assert json_request(server, "PUT", prefix + "frame_0002.png", headers=headers, body=b"complete")[0] == 204
        assert time.monotonic() - started < 0.2
        response = conn.getresponse()
        assert response.status == 400 and isinstance(json.loads(response.read())["error"], str)
        assert not list(directory.glob(".*.part-*"))
        assert not (directory / "frame_0001.png").exists()
        assert (directory / "frame_0002.png").read_bytes() == b"complete"
        assert restored.get(timeout=1) == restored.get(timeout=1) == 1.5
    finally:
        conn.close()


def test_concurrent_upload_reserves_name_and_frame_slot_and_releases_on_failure(server, project, tmp_path, monkeypatch):
    from keepframe.ae import api
    monkeypatch.setattr(api, "UPLOAD_IDLE_TIMEOUT", 0.5, raising=False)
    headers, job = running_upload_job(server, "render_frames")
    directory = tmp_path / "p1" / "ae" / "s1" / "v1"
    prefix = f'/api/ae/jobs/{job["id"]}/files/'
    for index in range(15):
        assert json_request(server, "PUT", prefix + f"frame_{index:04d}.png", headers=headers, body=b"png")[0] == 204
    conn = start_stalled_upload(server, headers, prefix + "frame_0015.png", directory)
    try:
        assert json_request(server, "PUT", prefix + "frame_0015.png", headers=headers, body=b"other")[0] == 409
        assert json_request(server, "PUT", prefix + "frame_0016.png", headers=headers, body=b"png")[0] == 413
        response = conn.getresponse()
        assert response.status == 400
        response.read()
    finally:
        conn.close()
    assert json_request(server, "PUT", prefix + "frame_0016.png", headers=headers, body=b"png")[0] == 204
    assert not list(directory.glob(".*.part-*"))


def test_authenticated_requests_refresh_seen_and_preserve_status_without_polling(server, project, monkeypatch):
    from keepframe.ae import devices
    headers, job = running_upload_job(server, "render_frames")
    current = [time.time()]
    monkeypatch.setattr(devices.time, "time", lambda: current[0])
    routes = server.ae_routes
    status = {"project_name": "Open.aep", "project_saved": True}
    routes.devices.touch(job["device"], status)
    prefix = f'/api/ae/jobs/{job["id"]}'
    calls = [("POST", prefix + "/progress", {"stage": "render", "done": 1, "total": 2}, None, 204),
             ("PUT", prefix + "/files/frame_0001.png", None, b"png", 204),
             ("GET", prefix + "/assets/e1.png", None, None, 200),
             ("GET", prefix + "/spec", None, None, 404),
             ("POST", "/api/ae/info", {"info": INFO}, None, 204),
             ("GET", "/api/ae/unknown", None, None, 404),
             ("POST", prefix + "/result", {"ok": False, "error": "upload-only test complete"}, None, 200)]
    for method, path, data, body, expected in calls:
        current[0] += 65
        assert request(server, method, path, data, body=body, headers=headers)[0] == expected
        row = routes.devices.list()[0]
        assert row["last_seen"] == current[0]
        assert {key: row[key] for key in status} == status
        assert routes.jobs.sweep({row["id"]: row["last_seen"]}, now=current[0]) == []
        assert routes.jobs.get(job["id"]).state == ("failed" if path.endswith("/result") else "running")


def test_upload_refreshes_seen_as_slow_body_arrives_for_over_sweep_threshold(server, project, tmp_path, monkeypatch):
    from keepframe.ae import devices
    headers, job = running_upload_job(server, "render_frames")
    current = [time.time()]
    monkeypatch.setattr(devices.time, "time", lambda: current[0])
    routes = server.ae_routes
    seen = queue.Queue()
    original = routes.devices.seen

    def record_seen(device_id, now=None):
        original(device_id, now)
        seen.put(current[0])

    monkeypatch.setattr(routes.devices, "seen", record_seen)
    directory = tmp_path / "p1" / "ae" / "s1" / "v1"
    path = f'/api/ae/jobs/{job["id"]}/files/frame_0001.png'
    conn = start_stalled_upload(server, headers, path, directory)
    try:
        assert seen.get(timeout=1) == current[0]  # Authentication.
        assert seen.get(timeout=1) == current[0]  # The initial five bytes.
        for _ in range(7):
            current[0] += 10
            conn.send(b"x")
            assert seen.get(timeout=1) == current[0]
            row = routes.devices.list()[0]
            assert routes.jobs.sweep({row["id"]: row["last_seen"]}, now=current[0]) == []
        assert routes.jobs.get(job["id"]).state == "running"
        conn.send(b"z" * 88)
        response = conn.getresponse()
        assert response.status == 204
        response.read()
    finally:
        conn.close()
    assert (directory / "frame_0001.png").read_bytes() == b"short" + b"x" * 7 + b"z" * 88


def test_sweep_prunes_progress_and_upload_bookkeeping(server, project, monkeypatch):
    from keepframe.ae import devices
    headers, job = running_upload_job(server, "render_frames")
    routes = server.ae_routes
    prefix = f'/api/ae/jobs/{job["id"]}'
    assert json_request(server, "POST", prefix + "/progress", {"stage": "render", "done": 1, "total": 2},
                        headers=headers)[0] == 204
    assert json_request(server, "PUT", prefix + "/files/frame_0001.png", headers=headers, body=b"png")[0] == 204
    assert job["id"] in routes._progress and job["id"] in routes._uploaded
    current = routes.devices.list()[0]["last_seen"] + 60
    monkeypatch.setattr(devices.time, "time", lambda: current)
    waits = iter((False, True))
    with monkeypatch.context() as patch:
        patch.setattr(routes._stop, "wait", lambda seconds: next(waits))
        routes._sweep()
    assert routes.jobs.get(job["id"]).state == "failed"
    assert job["id"] not in routes._progress and job["id"] not in routes._uploaded


@pytest.mark.parametrize("abandon", [False, True])
def test_failed_job_prunes_uploads_and_prevents_inflight_upload_from_replacing(server, project, tmp_path, abandon):
    headers, job = running_upload_job(server, "render_frames")
    routes = server.ae_routes
    directory = tmp_path / "p1" / "ae" / "s1" / "v1"
    prefix = f'/api/ae/jobs/{job["id"]}/files/'
    assert json_request(server, "PUT", prefix + "frame_0001.png", headers=headers, body=b"png")[0] == 204
    conn = start_stalled_upload(server, headers, prefix + "frame_0002.png", directory)
    try:
        if abandon:
            assert poll(server, headers) == (204, None)
        else:
            assert finish(server, headers, job, ok="yes")[0] == 400
        assert job["id"] not in routes._uploaded and job["id"] not in routes._uploading
        conn.send(b"x" * 95)
        response = conn.getresponse()
        assert response.status == 400
        response.read()
    finally:
        conn.close()
    assert not list(directory.glob(".*.part-*"))
    assert not (directory / "frame_0002.png").exists()
    assert job["id"] not in routes._uploaded and job["id"] not in routes._uploading


def test_server_request_failure_is_logged_without_headers(server, monkeypatch, caplog):
    _, headers = pair(server)

    def fail(*args):
        raise RuntimeError("simulated request error")

    monkeypatch.setattr(server.ae_routes.devices, "update_info", fail)
    assert json_request(server, "POST", "/api/ae/info", {"info": INFO}, headers=headers) == (
        500, {"error": "After Effects request failed"})
    assert any(record.name == "keepframe.ae" and record.exc_info for record in caplog.records)
    assert headers["Authorization"] not in caplog.text
    assert headers["Host"] not in caplog.text


@pytest.mark.parametrize("operation", ["mkdir", "write"])
def test_server_upload_failure_is_not_a_client_error(server, project, tmp_path, monkeypatch, caplog, operation):
    from keepframe.ae import api
    headers, job = running_upload_job(server, "render_frames")
    directory = tmp_path / "p1" / "ae" / "s1" / "v1"
    if operation == "mkdir":
        original = api.Path.mkdir

        def mkdir(path, *args, **kwargs):
            if path == directory:
                raise PermissionError(errno.EACCES, "simulated permission error")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(api.Path, "mkdir", mkdir)
        expected = 500
    else:
        original = api.os.fdopen

        class FullDisk:
            def __init__(self, fd):
                self.stream = original(fd, "wb")

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stream.close()

            def write(self, chunk):
                raise OSError(errno.ENOSPC, "simulated full disk")

        monkeypatch.setattr(api.os, "fdopen", lambda fd, mode: FullDisk(fd) if mode == "wb" else original(fd, mode))
        expected = 507
    status, error = json_request(server, "PUT", f'/api/ae/jobs/{job["id"]}/files/frame_0001.png',
                                 headers=headers, body=b"png")
    assert status == expected and isinstance(error["error"], str)
    assert not list(directory.glob(".*.part-*")) and not (directory / "frame_0001.png").exists()
    assert any(record.name == "keepframe.ae" and record.exc_info for record in caplog.records)


@pytest.mark.parametrize("failure", ["missing", "invalid"])
def test_final_spec_failure_names_problem_and_logs_route(server, project, tmp_path, caplog, failure):
    device, headers = pair(server)
    job = server.ae_routes.jobs.enqueue(device, "sync", "p1", "s1", "v1")
    path = scene_dir(tmp_path / "p1", "s1") / "assets/image.png"
    if failure == "missing":
        path.unlink()
    else:
        path.write_bytes(b"invalid image")
    route = f"/api/ae/jobs/{job.id}/spec"
    with caplog.at_level(logging.WARNING):
        status, body = json_request(server, "GET", route, headers=headers)
    assert status == (404 if failure == "missing" else 400)
    assert path.name in body["error"]
    records = [r for r in caplog.records if r.name == "keepframe.ae"]
    assert any(r.levelno == logging.WARNING and route in r.message and path.name in r.message for r in records)
    assert headers["Authorization"] not in caplog.text
    assert headers["Authorization"][7:] not in caplog.text
    assert headers["Host"] not in caplog.text


def test_final_revoke_fails_all_active_jobs_only_for_device(server, project):
    device, headers = pair(server)
    other, _ = pair(server)
    jobs = server.ae_routes.jobs
    running = jobs.enqueue(device, "sync", "p1", "s1", "v1")
    assert poll(server, headers)[1]["job"]["id"] == running.id
    queued = jobs.enqueue(device, "package", "p1", "s1", "v1")
    unrelated = jobs.enqueue(other, "sync", "p1", "s1", "v1")
    assert json_request(server, "DELETE", f"/api/ae/devices/{device}", browser=True)[0] == 204
    for job in jobs.state("p1", "s1")["jobs"]:
        if job["device"] == device:
            assert job["state"] == "failed"
            assert job["error"] == "After Effects was disconnected from this server"
            assert job["finished"] is not None
    assert jobs.get(queued.id).state == "failed"
    assert jobs.get(unrelated.id).state == "queued"
    assert poll(server, headers)[0] == 401


def test_final_repair_revokes_previous_device_only_after_success(server, project):
    device, headers = pair(server)
    other, other_headers = pair(server)
    queued = server.ae_routes.jobs.enqueue(device, "sync", "p1", "s1", "v1")
    payload = {"code": "invalid", "info": INFO, "previous_device_id": device}
    assert json_request(server, "POST", "/api/ae/pair", payload, headers=EXTENSION)[0] == 401
    assert poll(server, headers)[0] == 200
    code = json_request(server, "POST", "/api/ae/codes", browser=True)[1]["code"]
    payload["code"] = code
    payload["info"] = INFO | {"fonts": None}
    assert json_request(server, "POST", "/api/ae/pair", payload, headers=EXTENSION)[0] == 400
    assert server.ae_routes.devices.authenticate(headers["Authorization"][7:]) is not None
    payload["info"] = INFO
    status, paired = json_request(server, "POST", "/api/ae/pair", payload, headers=EXTENSION)
    assert status == 200
    assert poll(server, headers)[0] == 401
    assert server.ae_routes.jobs.get(queued.id).error == "After Effects was disconnected from this server"
    assert poll(server, other_headers)[0] == 204
    assert poll(server, EXTENSION | {"Authorization": "Bearer " + paired["token"]})[0] == 204


@pytest.fixture
def fake_verify(monkeypatch):
    from keepframe.ae import api
    calls, release = queue.Queue(), threading.Event()
    release.set()

    def fake(scene, directory, ae_frames, out_dir, *, masked):
        calls.put((scene, directory, ae_frames, out_dir, masked))
        assert release.wait(5)
        out_dir.mkdir(parents=True, exist_ok=True)
        worst = {"frame": 0, "l1": 0.01}
        for kind in ("ae", "kf", "diff"):
            name = f"f0000_{kind}.png"
            png(out_dir / name, 20, 12)
            worst[kind] = name
        report = {"passed": True, "mean": 0.005, "max": 0.01,
                  "thresholds": {"mean": 0.025, "frame": 0.04},
                  "frames": [{"frame": f, "l1": 0.005} for f in ae_frames],
                  "worst": [worst],
                  "notes": [f"{font['name']}: {font['font']} instead of {font['requested']} — text region differs by 10.0%"
                            for font in masked.values()],
                  "masked": [{"id": eid, **font, "worst_l1": 0.1} for eid, font in masked.items()]}
        (out_dir / "verify.json").write_text(json.dumps(report))
        return report

    monkeypatch.setattr(api, "verify", fake, raising=False)
    try:
        yield calls, release
    finally:
        release.set()


def verify_request(server, **changes):
    return json_request(server, "POST", "/api/ae/verify", {"project": "p1", "scene": "s1", **changes},
                        browser=True)


def running_verify(server):
    _, headers, sync = running_sync(server)
    assert finish(server, headers, sync, ok=True, result={"applied": True})[0] == 200
    status, value = verify_request(server)
    assert status == 202
    job = value["job"]
    assert poll(server, headers)[1]["job"]["id"] == job["id"]
    return headers, job


def upload_verify_frames(server, headers, job, *, skip=()):
    for frame in job["params"]["frames"]:
        if frame not in skip:
            assert json_request(server, "PUT", f'/api/ae/jobs/{job["id"]}/files/frame_{frame:04d}.png',
                                headers=headers, body=b"png")[0] == 204


def wait_verify(server, expected="done"):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        report = state(server)["verify"]
        if report["state"] == expected:
            return report
        time.sleep(0.01)
    pytest.fail(f"verification did not reach {expected}: {report}")


def test_verify_enqueues_render_frames_with_sampled_frames(server, project, tmp_path):
    assert verify_request(server) == (409, {"error": "no connected After Effects"})
    device, headers, sync = running_sync(server)
    assert finish(server, headers, sync, ok=True, result={"applied": True})[0] == 200
    status, value = verify_request(server, device=device, version="v1")
    assert status == 202
    job = value["job"]
    assert (job["device"], job["project"], job["scene"], job["version"], job["kind"], job["depends_on"]) == (
        device, "p1", "s1", "v1", "render_frames", None)
    assert job["params"] == {"frames": [0, 4, 8, 12, 16, 20, 24, 28, 31, 35, 39, 43, 47, 51, 55, 59],
                             "tag": "keepframe:p1/s1"}
    assert state(server)["verify"] == {"job": job["id"], "version": "v1", "state": "rendering"}
    new_version(tmp_path / "p1", "s1", project.model_copy(update={"frames": 5}), "shorter")
    status, value = verify_request(server)
    assert status == 202 and value["job"]["version"] == "v2"
    assert value["job"]["params"]["frames"] == [0, 1, 2, 3, 4]
    dependency = server.ae_routes.jobs.get(value["job"]["depends_on"])
    assert dependency.kind == "sync" and dependency.version == "v2"
    assert verify_request(server, device="missing")[0] == 404


def test_render_frames_result_starts_verification_and_state_reports_it(server, project, tmp_path, fake_verify, caplog):
    calls, release = fake_verify
    release.clear()
    headers, job = running_verify(server)
    assert state(server)["verify"]["state"] == "rendering"
    upload_verify_frames(server, headers, job)
    with caplog.at_level(logging.INFO):
        status, result = finish(server, headers, job, ok=True, result={"frames": job["params"]["frames"]})
        assert status == 200 and result["job"]["state"] == "done"
        scene, directory, frames, _, masked = calls.get(timeout=2)
        assert scene == project and directory == scene_dir(tmp_path / "p1", "s1")
        assert set(frames) == set(job["params"]["frames"])
        assert all(path.name == f"frame_{f:04d}.png" and path.read_bytes() == b"png"
                   and path.parent.parent == tmp_path / "p1" / "ae" / "s1" / "v1" for f, path in frames.items())
        assert masked == {"title": {"name": "title · text", "font": "Arial", "requested": "Example"}}
        assert state(server)["verify"] == {"job": job["id"], "version": "v1", "state": "verifying"}
        assert poll(server, headers) == (204, None)
        release.set()
        report = wait_verify(server)
    assert report["job"] == job["id"] and report["version"] == "v1"
    assert report["passed"] and report["mean"] == 0.005 and report["finished"] >= result["job"]["finished"]
    assert report["masked"] == [{"id": "title", "name": "title · text", "font": "Arial",
                                 "requested": "Example", "worst_l1": 0.1}]
    assert report["notes"] == ["title · text: Arial instead of Example — text region differs by 10.0%"]
    stored = json.loads((tmp_path / "p1" / "ae" / "s1" / "v1" / "verify" / "verify.json").read_text())
    assert stored == {key: value for key, value in report.items() if key != "state"}
    path = "/api/ae/verify-image?project=p1&scene=s1&version=v1&name=f0000_ae.png"
    for origin in ({}, {"Origin": "https://evil.example"}):
        status, response_headers, body = request(server, "GET", path, headers=origin)
        assert status == 200 and response_headers["content-type"] == "image/png" and body.startswith(b"\x89PNG")
        assert not any(key.lower().startswith("access-control-") for key in response_headers)
    assert request(server, "GET", path, headers={"Host": "evil.example"})[0] == 403
    logs = [r.getMessage() for r in caplog.records if r.name == "keepframe.ae" and r.levelno == logging.INFO]
    assert any("verification start" in text and "p1" in text and "v1" in text for text in logs)
    assert any("verification finish" in text and "0.005" in text and "True" in text for text in logs)
    assert not any(str(tmp_path) in text or headers["Authorization"][7:] in text for text in logs)
    assert job["id"] not in server.ae_routes._uploaded


@pytest.mark.parametrize("case", ["upload", "result", "extra", "combined", "wrong_type", "not_list", "no_result"])
def test_result_with_missing_frames_fails_without_verifying(server, project, tmp_path, fake_verify, case):
    calls, _ = fake_verify
    headers, job = running_verify(server)
    frames = job["params"]["frames"]
    # A stale file on disk is not an upload for this job.
    if case == "upload":
        directory = tmp_path / "p1" / "ae" / "s1" / "v1"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "frame_0012.png").write_bytes(b"stale")
    upload_verify_frames(server, headers, job, skip=(12,) if case == "upload" else ())
    reported = {"upload": frames, "result": [f for f in frames if f != 12], "extra": frames + [40],
                "combined": [f for f in frames if f != 12] + [40],
                "wrong_type": [False, *frames[1:]], "not_list": "frames", "no_result": None}[case]
    status, value = finish(server, headers, job, ok=True, result={"frames": reported})
    assert status in (200, 400) and server.ae_routes.jobs.get(job["id"]).state == "failed"
    if case in ("upload", "result"):
        assert state(server)["verify"]["error"] == "frames missing from AE: 12"
    elif case == "extra":
        assert state(server)["verify"]["error"] == "frames missing from AE: 40"
    elif case == "combined":
        assert state(server)["verify"]["error"] == "frames missing from AE: 12, 40"
    assert state(server)["verify"]["state"] == "failed" and calls.empty()
    assert job["id"] not in server.ae_routes._uploaded
    assert poll(server, headers) == (204, None)


def test_verify_uses_the_job_version(server, project, tmp_path, fake_verify):
    headers, job = running_verify(server)
    edited = project.model_copy(update={"size": (640, 360), "frames": 120})
    new_version(tmp_path / "p1", "s1", edited, "new size")
    upload_verify_frames(server, headers, job)
    assert finish(server, headers, job, ok=True, result={"frames": job["params"]["frames"]})[0] == 200
    assert wait_verify(server)["version"] == "v1"
    assert fake_verify[0].get(timeout=2)[0] == project
    assert not (tmp_path / "p1" / "ae" / "s1" / "v2" / "verify").exists()


def test_interrupted_verification_is_reported(server, project, tmp_path):
    headers, job = running_verify(server)
    # Emulate a crash after the render job was persisted as done, before the server verifier finished.
    server.ae_routes.jobs.finish(job["id"], True, {"frames": job["params"]["frames"]})
    directory = tmp_path / "p1" / "ae" / "s1" / "v1" / "verify"
    directory.mkdir(parents=True)
    (directory / "verify.json").write_text(json.dumps({"job": "older", "version": "v1", "passed": True}))
    assert state(server)["verify"] == {"job": job["id"], "version": "v1", "state": "interrupted"}
    (directory / "verify.json").unlink()
    # The latest render job must still be found beyond the ten recent-job rows.
    for _ in range(11):
        server.ae_routes.jobs.enqueue(job["device"], "sync", "p1", "s1", "v1")
    assert state(server)["verify"]["state"] == "interrupted"


@pytest.mark.parametrize("error,expected", [
    (ValueError("frame 3 from AE is missing or not an image"), "frame 3 from AE is missing or not an image"),
    (FileNotFoundError(errno.ENOENT, "not found", "/private/assets/missing.png"), "file not found: missing.png"),
    (RuntimeError("failure in /private/workspace"), "verification failed: RuntimeError"),
])
def test_verifier_error_is_shown(server, project, tmp_path, monkeypatch, caplog, error, expected):
    from keepframe.ae import api

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(api, "verify", fail, raising=False)
    headers, job = running_verify(server)
    upload_verify_frames(server, headers, job)
    assert finish(server, headers, job, ok=True, result={"frames": job["params"]["frames"]})[0] == 200
    report = wait_verify(server, "failed")
    assert report["error"] == expected and "/private" not in report["error"]
    saved = json.loads((tmp_path / "p1" / "ae" / "s1" / "v1" / "verify" / "verify.json").read_text())
    assert saved["error"] == expected and saved["job"] == job["id"] and saved["finished"] is not None
    if isinstance(error, RuntimeError):
        assert any(r.name == "keepframe.ae" and r.exc_info for r in caplog.records)


def test_verify_image_rejects_other_names(server, project, tmp_path):
    directory = tmp_path / "p1" / "ae" / "s1" / "v1" / "verify"
    directory.mkdir(parents=True)
    png(directory / "f0000_ae.png", 20, 12)
    png(directory / "f012345_diff.png", 20, 12)
    assert request(server, "GET", "/api/ae/verify-image?project=p1&scene=s1&version=v1&name=f012345_diff.png")[0] == 200
    for name in ("../verify.json", "f1_ae.png", "f1234567_ae.png", "f0000_other.png", "verify.json"):
        assert json_request(server, "GET", "/api/ae/verify-image?project=p1&scene=s1&version=v1&name=" + quote(name))[0] == 404
    assert json_request(server, "GET", "/api/ae/verify-image?project=other&scene=s1&version=v1&name=f0000_ae.png")[0] == 404
    assert json_request(server, "GET", "/api/ae/verify-image?project=p1&scene=s1&version=missing&name=f0000_ae.png")[0] == 404
    assert json_request(server, "GET", "/api/ae/verify-image?project=p1&scene=s1&name=f0000_ae.png")[0] == 404
    outside = tmp_path / "outside.png"
    png(outside, 20, 12)
    (directory / "f0000_ae.png").unlink()
    (directory / "f0000_ae.png").symlink_to(outside)
    assert json_request(server, "GET", "/api/ae/verify-image?project=p1&scene=s1&version=v1&name=f0000_ae.png")[0] == 404


def test_upload_names_allow_six_digits(server, project, tmp_path):
    headers, job = running_upload_job(server, "render_frames")
    prefix = f'/api/ae/jobs/{job["id"]}/files/'
    for name in ("frame_012345.png", "frame_12345.png"):
        assert json_request(server, "PUT", prefix + name, headers=headers, body=b"png")[0] == 204
        assert (tmp_path / "p1" / "ae" / "s1" / "v1" / name).read_bytes() == b"png"
    assert json_request(server, "PUT", prefix + "frame_1234567.png", headers=headers, body=b"png")[0] == 404


def test_verify_masks_scene_text_only_and_clears_old_outputs(server, project, tmp_path, fake_verify, monkeypatch):
    from keepframe.ae import api

    def spec_with_unknown_text(*args, **kwargs):
        spec = comp_spec(*args, **kwargs)
        spec["layers"].append({"id": "kf:unknown", "kind": "text", "name": "Unknown",
                               "source": {"font": {"substituted": True, "family": "Arial"}}})
        return spec

    monkeypatch.setattr(api, "comp_spec", spec_with_unknown_text)
    project.elements.append(Element(id="image_text", kind="text", visible=(0, 59),
                                    canonical=Canonical(width=20, height=12, text="Raster text", texture="assets/image.png")))
    new_version(tmp_path / "p1", "s1", project, "companion")
    device, headers, sync = running_sync(server)
    assert sync["version"] == "v2"
    fonts = [{"family": "Example", "style": "Regular", "postscript": "Example-Regular"}]
    assert json_request(server, "POST", "/api/ae/info", {"info": INFO | {"fonts": fonts}}, headers=headers)[0] == 204
    assert finish(server, headers, sync, ok=True, result={"applied": True})[0] == 200
    job = verify_request(server, device=device)[1]["job"]
    assert poll(server, headers)[1]["job"]["id"] == job["id"]
    directory = tmp_path / "p1" / "ae" / "s1" / "v2" / "verify"
    directory.mkdir(parents=True)
    for name in ("f0059_ae.png", "f0059_kf.png", "f0059_diff.png", "f_old_diff.png", "verify.json"):
        (directory / name).write_text("old")
    upload_verify_frames(server, headers, job)
    assert finish(server, headers, job, ok=True, result={"frames": job["params"]["frames"]})[0] == 200
    report = wait_verify(server)
    assert fake_verify[0].get(timeout=2)[4] == {} and report["masked"] == []
    assert sorted(p.name for p in directory.iterdir()) == ["f0000_ae.png", "f0000_diff.png", "f0000_kf.png", "verify.json"]


def test_close_does_not_join_verifier_or_publish_after_shutdown(server, project, tmp_path, fake_verify):
    calls, release = fake_verify
    release.clear()
    headers, job = running_verify(server)
    upload_verify_frames(server, headers, job)
    assert finish(server, headers, job, ok=True, result={"frames": job["params"]["frames"]})[0] == 200
    calls.get(timeout=2)
    threads = [t for t in threading.enumerate() if job["id"] in t.name]
    assert len(threads) == 1 and threads[0].daemon
    server.ae_routes.close()
    assert threads[0].is_alive()
    release.set()
    threads[0].join(timeout=2)
    assert not threads[0].is_alive()
    directory = tmp_path / "p1" / "ae" / "s1" / "v1" / "verify"
    assert not list(directory.glob("*.png")) and not (directory / "verify.json").exists()


def test_verifications_run_one_at_a_time(server, project, fake_verify):
    calls, release = fake_verify
    release.clear()
    headers, first = running_verify(server)
    upload_verify_frames(server, headers, first)
    assert finish(server, headers, first, ok=True, result={"frames": first["params"]["frames"]})[0] == 200
    calls.get(timeout=2)
    second = verify_request(server)[1]["job"]
    assert poll(server, headers)[1]["job"]["id"] == second["id"]
    upload_verify_frames(server, headers, second)
    assert finish(server, headers, second, ok=True, result={"frames": second["params"]["frames"]})[0] == 200
    assert calls.empty() and state(server)["verify"]["state"] == "verifying"
    release.set()
    calls.get(timeout=2)
    assert wait_verify(server)["job"] == second["id"]


def test_report_write_failure_is_shown(server, project, fake_verify, monkeypatch):
    from keepframe.ae import api
    original = api.os.replace

    def fail_report(source, destination):
        if api.Path(destination).name == "verify.json":
            raise PermissionError(errno.EACCES, "cannot write /private/report")
        return original(source, destination)

    monkeypatch.setattr(api.os, "replace", fail_report)
    headers, job = running_verify(server)
    upload_verify_frames(server, headers, job)
    assert finish(server, headers, job, ok=True, result={"frames": job["params"]["frames"]})[0] == 200
    assert wait_verify(server, "failed")["error"] == "verification failed: PermissionError"


def test_queued_verification_keeps_its_uploaded_frames(server, project, monkeypatch):
    from keepframe.ae import api
    entered, release, values = threading.Event(), threading.Event(), queue.Queue()

    def fake(scene, directory, ae_frames, out_dir, *, masked):
        entered.set()
        assert release.wait(5)
        values.put(ae_frames[0].read_bytes())
        return {"passed": True, "mean": 0, "max": 0, "frames": [], "worst": [], "notes": [], "masked": []}

    monkeypatch.setattr(api, "verify", fake)
    headers, first = running_verify(server)
    try:
        upload_verify_frames(server, headers, first)
        assert finish(server, headers, first, ok=True, result={"frames": first["params"]["frames"]})[0] == 200
        assert entered.wait(2)
        second = verify_request(server)[1]["job"]
        assert poll(server, headers)[1]["job"]["id"] == second["id"]
        upload_verify_frames(server, headers, second)
        assert json_request(server, "PUT", f'/api/ae/jobs/{second["id"]}/files/frame_0000.png',
                            headers=headers, body=b"second render")[0] == 204
        assert finish(server, headers, second, ok=True, result={"frames": second["params"]["frames"]})[0] == 200
        release.set()
        assert values.get(timeout=2) == b"png"
        assert values.get(timeout=2) == b"second render"
        assert wait_verify(server)["job"] == second["id"]
    finally:
        release.set()


def test_older_verification_cannot_replace_newer_report(server, project, fake_verify, monkeypatch):
    routes = server.ae_routes
    original = routes._run_verification
    entered, release = threading.Event(), threading.Event()
    headers, first = running_verify(server)

    def delay_first(job, *args):
        if job.id == first["id"]:
            entered.set()
            assert release.wait(5)
        return original(job, *args)

    monkeypatch.setattr(routes, "_run_verification", delay_first)
    try:
        upload_verify_frames(server, headers, first)
        assert finish(server, headers, first, ok=True, result={"frames": first["params"]["frames"]})[0] == 200
        assert entered.wait(2)
        second = verify_request(server)[1]["job"]
        assert poll(server, headers)[1]["job"]["id"] == second["id"]
        upload_verify_frames(server, headers, second)
        assert finish(server, headers, second, ok=True, result={"frames": second["params"]["frames"]})[0] == 200
        assert wait_verify(server)["job"] == second["id"]
        thread = next(t for t in threading.enumerate() if first["id"] in t.name)
        release.set()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert state(server)["verify"]["state"] == "done"
        assert state(server)["verify"]["job"] == second["id"]
    finally:
        release.set()
