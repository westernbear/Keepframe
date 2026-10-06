"""PYTEST_DONT_REWRITE: do not expose pairing credentials in assertion output."""

import http.client
import hashlib
import json
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

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
    ("POST", "/api/ae/codes"), ("GET", "/api/ae/devices"),
    ("DELETE", "/api/ae/devices/missing"), ("POST", "/api/ae/send"),
    ("GET", "/api/ae/state?project=p1&scene=s1"),
])
def test_browser_routes_reject_cross_origin_first(server, method, path):
    status, error = json_request(server, method, path, headers={"Origin": "https://evil.example"})
    assert status == 403 and isinstance(error["error"], str)


def test_pairing_and_same_origin_device_routes(server):
    device, headers = pair(server)
    status, listed = json_request(server, "GET", "/api/ae/devices", browser=True)
    assert status == 200 and listed["devices"][0]["id"] == device
    assert listed["devices"][0]["connected"] is False
    status, _ = json_request(server, "POST", "/api/ae/info", {"info": INFO | {"ae_version": "26.0"}},
                             headers=headers)
    assert status == 204
    status, listed = json_request(server, "GET", "/api/ae/devices", browser=True)
    assert listed["devices"][0]["ae_version"] == "26.0"
    assert json_request(server, "DELETE", f"/api/ae/devices/{device}", browser=True)[0] == 204
    status, error = json_request(server, "POST", "/api/ae/info", {"info": INFO}, headers=headers)
    assert status == 401
    assert error == {"error": "device is not paired; pair again from the Keepframe web page"}


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
    assert set(snapshot) == {"jobs", "last_synced", "devices", "progress"}
    assert len(snapshot["jobs"]) == 2 and snapshot["last_synced"] == snapshot["progress"] == {}


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
    assert json_request(server, "POST", prefix + "/result", body=b"{", headers=headers) == (400, {"error": "bad json"})
    assert finish(server, headers, job, ok="yes")[0] == 400
    for value in ({}, {"stage": "sync", "done": -1, "total": 2}, {"stage": "sync", "done": 3, "total": 2},
                  {"stage": [], "done": 1, "total": 2}):
        status, error = json_request(server, "POST", prefix + "/progress", value, headers=headers)
        assert status == 400 and isinstance(error["error"], str)


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
    assert finish(server, headers, job, ok=True)[0] == 200
    assert json_request(server, "PUT", prefix + name, headers=headers, body=data)[0] == 409
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


def test_upload_replace_failure_discards_temp_and_keeps_existing_file(server, project, tmp_path, monkeypatch):
    from keepframe.ae import api
    headers, job = running_upload_job(server, "package")
    path = f'/api/ae/jobs/{job["id"]}/files/project.zip'
    assert json_request(server, "PUT", path, headers=headers, body=b"original")[0] == 204
    original = api.os.replace

    def fail_upload(source, destination):
        if ".part-" in str(source):
            raise OSError("simulated disk error")
        original(source, destination)

    monkeypatch.setattr(api.os, "replace", fail_upload)
    status, error = json_request(server, "PUT", path, headers=headers, body=b"replacement")
    assert status == 400 and isinstance(error["error"], str)
    directory = tmp_path / "p1" / "ae" / "s1" / "v1"
    assert (directory / "project.zip").read_bytes() == b"original"
    assert list(directory.iterdir()) == [directory / "project.zip"]


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
