import json
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError

def start(tmp_path, **kwargs):
    from keepframe.web.server import make_server
    srv = make_server(tmp_path, port=0, **kwargs)
    import threading
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv

def get(srv, path):
    url = f"http://127.0.0.1:{srv.server_address[1]}{path}"
    try:
        with urlopen(url) as r:
            return r.status, r.headers.get_content_type(), r.read()
    except HTTPError as e:
        return e.code, e.headers.get_content_type(), e.read()

def test_admin_is_404_without_flag(tmp_path):
    srv = start(tmp_path)
    code, ctype, body = get(srv, "/admin")
    srv.shutdown()
    assert code == 404
    assert json.loads(body)["error"] == "로컬판에는 이 화면이 없습니다."

def test_missing_page_is_404_spa_and_missing_api_is_404_json(tmp_path):
    srv = start(tmp_path)
    code, ctype, body = get(srv, "/nope")
    api = get(srv, "/api/nope")
    srv.shutdown()
    assert (code, ctype) == (404, "text/html") and b'<div id="root">' in body   # the SPA's not-found page
    assert api[0] == 404 and json.loads(api[2])["error"] == "not found"


def test_status_reports_cuda_flag(tmp_path):
    srv = start(tmp_path)
    code, _, body = get(srv, "/api/status")
    srv.shutdown()
    assert code == 200
    data = json.loads(body)
    assert data["cuda"] in (True, False)
    assert data["device"] in ("cpu", "cuda")
    if not data["cuda"]:
        assert data["device"] == "cpu"


def test_root_is_landing_and_library_moved(tmp_path):
    srv = start(tmp_path)
    root_code, _, root = get(srv, "/")
    lib_code, _, lib = get(srv, "/library")
    srv.shutdown()
    assert root_code == 200
    assert b'<div id="root">' in root and b"/static/app/assets/" in root   # the SPA renders the landing (browser test)
    assert lib_code == 200
    assert b'<div id="root">' in lib   # the SPA renders the library (browser test)


def test_demo_redirects_to_review_and_serves_state(tmp_path):
    import http.client
    import json

    srv = start(tmp_path)
    port = srv.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port)
    conn.request("GET", "/demo")
    res = conn.getresponse()
    loc = res.getheader("location")
    res.read()
    conn.close()
    assert res.status == 302
    assert loc == "/review?project=demo&scene=synth7"
    code, _, body = get(srv, "/api/state?project=demo&scene=synth7")
    code2, _, frame = get(srv, "/frame/orig/0?project=demo&scene=synth7")
    code3, _, thumb = get(srv, "/api/projects/demo/frame/0")
    srv.shutdown()
    assert code == 200
    state = json.loads(body)
    assert state["scene"]["id"] == "synth7"
    assert state["scene"]["frames"] == 24
    assert code2 == 200
    assert frame.startswith(b"\x89PNG\r\n\x1a\n")
    assert code3 == 404


def test_spa_entry_revalidates_and_hashed_assets_are_immutable(tmp_path):
    import re
    from keepframe.web.server import STATIC

    html = (STATIC / "app" / "index.html").read_text(encoding="utf-8")
    js = re.search(r'src="(/static/app/assets/[^"]+\.js)"', html).group(1)
    css = re.search(r'href="(/static/app/assets/[^"]+\.css)"', html).group(1)
    srv = start(tmp_path)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    heads = {}
    for path in ("/static/app/index.html", js, css, "/static/logo.png"):
        with urlopen(base + path) as r:
            assert r.status == 200
            heads[path] = (r.headers.get("cache-control"), r.headers.get("content-type"))
    srv.shutdown()
    assert "no-cache" in heads["/static/app/index.html"][0]
    assert heads[js] == ("public, max-age=31536000, immutable", "application/javascript; charset=utf-8")
    assert heads[css] == ("public, max-age=31536000, immutable", "text/css; charset=utf-8")
    png_cache = heads["/static/logo.png"][0]
    assert png_cache and "max-age" in png_cache and "immutable" not in png_cache


def test_logo_is_served(tmp_path):
    srv = start(tmp_path)
    code, ctype, body = get(srv, "/static/logo.png")
    srv.shutdown()
    assert code == 200
    assert ctype == "image/png"
    assert body[:8] == b"\x89PNG\r\n\x1a\n"


def test_mask_correction_decodes_payload_before_dispatch(tmp_path, monkeypatch):
    import base64
    import threading
    from types import SimpleNamespace

    from keepframe.ir.store import init_project, scene_dir
    from keepframe.ir.synth import make_synthetic_scene
    from keepframe.web.server import ReviewState

    root = tmp_path / "p1"
    scene = make_synthetic_scene(root / "scenes" / "s1", seed=3, with_text=False, frames=4)
    init_project(root, {"file": "source.mp4"}, scene.model_copy(update={"id": "s1"}))
    stages = scene_dir(root, "s1") / "stages"
    stages.mkdir()
    (stages / "ids.json").write_text(json.dumps({"o1": "e1"}))
    dispatched = threading.Event()
    captured = {}

    def fake_set_region_mask(root_, scene_id, frame, mask, object_id, note):
        captured["mask"] = Path(mask).read_bytes()
        dispatched.set()
        return SimpleNamespace(id="v2")

    monkeypatch.setattr("keepframe.web.server.corrections.set_region_mask", fake_set_region_mask)
    state = ReviewState(root, "s1")
    state.run_correction(
        "mask",
        {
            "frame": 0,
            "object_id": "e1",
            "mask_png_base64": base64.b64encode(b"mask-bytes").decode("ascii"),
        },
    )

    assert dispatched.wait(1)
    assert captured["mask"] == b"mask-bytes"


def _send(srv, method, path, origin=True):
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    req = Request(base + path, method=method, data=b"" if method == "POST" else None, headers={"Origin": base} if origin else {})
    try:
        with urlopen(req) as r:
            return r.status, json.loads(r.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


class _IdleRunner:   # jobs stay queued: a stand-in for a running analyze
    def enqueue(self, job, *, spec=None, fn=None):
        pass


def test_delete_project_checks_origin_id_and_running_jobs(tmp_path, monkeypatch):
    from keepframe.jobs import JobStore
    from keepframe.web.workspace import write_meta
    store = JobStore(runner=_IdleRunner())
    monkeypatch.setattr("keepframe.web.server.JOBS", store)
    for pid in ("p1", "p2"):
        (tmp_path / pid).mkdir()
        (tmp_path / pid / "meta.json").write_text(json.dumps({"id": pid, "title": pid, "status": "uploaded"}))
    job = store.submit("analyze", lambda: {}, project_id="p2")
    write_meta(tmp_path, "p2", status="analyzing", job_id=job.id)
    srv = start(tmp_path)
    try:
        assert _send(srv, "DELETE", "/api/projects/p1", origin=False)[0] == 403
        assert _send(srv, "DELETE", "/api/projects/..%2Fx")[0] == 400
        assert _send(srv, "DELETE", "/api/projects/nope")[0] == 404
        assert _send(srv, "DELETE", "/api/projects/p2") == (409, {"error": "job_running"})
        assert _send(srv, "DELETE", "/api/projects/p1") == (200, {"deleted": "p1"})
        assert not (tmp_path / "p1").exists()
        assert [p["id"] for p in json.loads(get(srv, "/api/projects")[2])["projects"]] == ["p2"]
        assert _send(srv, "POST", f"/api/jobs/{job.id}/cancel", origin=False)[0] == 403
        code, body = _send(srv, "POST", f"/api/jobs/{job.id}/cancel")
        assert code == 202 and body["job"]["cancel_requested"] is True
        assert _send(srv, "POST", "/api/jobs/missing/cancel")[0] == 404
        job.status = "done"
        assert _send(srv, "POST", f"/api/jobs/{job.id}/cancel") == (409, {"error": "not_running"})
    finally:
        srv.shutdown()
