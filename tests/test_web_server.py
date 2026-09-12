import json
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError

def start(tmp_path):
    from keepframe.web.server import make_server
    srv = make_server(tmp_path, port=0)
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

def test_missing_page_is_404_json(tmp_path):
    srv = start(tmp_path)
    code, _, body = get(srv, "/nope")
    srv.shutdown()
    assert code == 404
    assert json.loads(body)["error"] == "not found"


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
    assert b"land-hero" in root
    assert "유지할 것과 바꿀 것을 지정하세요".encode("utf-8") in root
    assert lib_code == 200
    assert b"project-list" in lib


def test_logo_is_served(tmp_path):
    srv = start(tmp_path)
    code, ctype, body = get(srv, "/static/logo.png")
    srv.shutdown()
    assert code == 200
    assert ctype == "image/png"
    assert body[:8] == b"\x89PNG\r\n\x1a\n"
