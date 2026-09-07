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
