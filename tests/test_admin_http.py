import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from refstudio.web.server import make_server
from refstudio.admin.memory import MemoryAdmin
from refstudio.admin.auth import MemoryAuth
import threading


def start_admin(tmp_path):
    srv = make_server(tmp_path, port=0, admin=True, admin_svc=MemoryAdmin(), admin_auth=MemoryAuth())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def post(srv, path, obj, cookie=None):
    req = Request(f"http://127.0.0.1:{srv.server_address[1]}{path}",
                  data=json.dumps(obj).encode(), method="POST",
                  headers={"content-type": "application/json"})
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urlopen(req) as r:
            return r.status, r.headers, json.loads(r.read())
    except HTTPError as e:
        return e.code, e.headers, json.loads(e.read())


def test_login_and_tenants(tmp_path):
    srv = start_admin(tmp_path)
    code, headers, body = post(srv, "/admin/api/login", {"email": "mina@ref.studio", "password": "dev-admin"})
    assert code == 200
    cookie = headers.get("Set-Cookie")
    assert "refstudio_admin=" in cookie
    req = Request(f"http://127.0.0.1:{srv.server_address[1]}/admin/api/policy")
    req.add_header("Cookie", cookie.split(";")[0])
    with urlopen(req) as r:
        pol = json.loads(r.read())
    srv.shutdown()
    assert pol["retry_cap"] == 4 and pol["asset_gen_cap"] == 2


def test_quarantine_keys_and_no_video(tmp_path):
    srv = start_admin(tmp_path)
    code, headers, _ = post(srv, "/admin/api/login", {"email": "mina@ref.studio", "password": "dev-admin"})
    cookie = headers.get("Set-Cookie").split(";")[0]
    req = Request(f"http://127.0.0.1:{srv.server_address[1]}/admin/api/quarantine")
    req.add_header("Cookie", cookie)
    with urlopen(req) as r:
        rows = json.loads(r.read())["items"]
    assert set(rows[0]) <= {"id", "filename", "tenant_id", "rejected_at", "reason"}
    req2 = Request(f"http://127.0.0.1:{srv.server_address[1]}/admin/api/quarantine/q1/video")
    req2.add_header("Cookie", cookie)
    try:
        urlopen(req2)
        assert False, "video must 404"
    except HTTPError as e:
        assert e.code == 404
    req3 = Request(f"http://127.0.0.1:{srv.server_address[1]}/admin/api/audit", method="DELETE")
    req3.add_header("Cookie", cookie)
    try:
        urlopen(req3)
        assert False
    except HTTPError as e:
        assert e.code == 404
    srv.shutdown()
