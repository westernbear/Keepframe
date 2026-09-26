import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from keepframe.web.server import make_server
from keepframe.admin.memory import MemoryAdmin
from keepframe.admin.auth import MemoryAuth
import threading
ADMIN_USERS = {"mina@keepframe.app": "dev-admin"}


def start_admin(tmp_path):
    srv = make_server(tmp_path, port=0, admin=True, admin_svc=MemoryAdmin(), admin_auth=MemoryAuth(ADMIN_USERS))
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
    code, headers, body = post(srv, "/admin/api/login", {"email": "mina@keepframe.app", "password": "dev-admin"})
    assert code == 200
    cookie = headers.get("Set-Cookie")
    assert "keepframe_admin=" in cookie
    req = Request(f"http://127.0.0.1:{srv.server_address[1]}/admin/api/policy")
    req.add_header("Cookie", cookie.split(";")[0])
    with urlopen(req) as r:
        pol = json.loads(r.read())
    srv.shutdown()
    assert pol["retry_cap"] == 4 and pol["asset_gen_cap"] == 2


def test_logout_requires_cookie(tmp_path):
    srv = start_admin(tmp_path)
    code, _, body = post(srv, "/admin/api/logout", {})
    srv.shutdown()
    assert code == 401
    assert body.get("error") == "unauthorized"


def test_quarantine_keys_and_no_video(tmp_path):
    srv = start_admin(tmp_path)
    code, headers, _ = post(srv, "/admin/api/login", {"email": "mina@keepframe.app", "password": "dev-admin"})
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

def test_unauthenticated_admin_apis_return_401(tmp_path):
    srv = start_admin(tmp_path)
    try:
        for path in ("/admin/api/policy", "/admin/api/tenants", "/admin/api/jobs", "/admin/api/quarantine", "/admin/api/audit", "/admin/api/llm"):
            req = Request(f"http://127.0.0.1:{srv.server_address[1]}{path}")
            try:
                urlopen(req)
                assert False, f"unauthenticated GET succeeded: {path}"
            except HTTPError as error:
                assert error.code == 401
    finally:
        srv.shutdown()
        srv.server_close()


def test_logout_invalidates_authenticated_session(tmp_path):
    srv = start_admin(tmp_path)
    try:
        code, headers, _ = post(srv, "/admin/api/login", {"email": "mina@keepframe.app", "password": "dev-admin"})
        cookie = headers.get("Set-Cookie").split(";")[0]
        logout, _, _ = post(srv, "/admin/api/logout", {}, cookie)
        req = Request(f"http://127.0.0.1:{srv.server_address[1]}/admin/api/policy")
        req.add_header("Cookie", cookie)
        try:
            urlopen(req)
            assert False, "logged-out session remained valid"
        except HTTPError as error:
            assert error.code == 401
        assert code == logout == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_admin_audit_csv_and_ordered_suspension(tmp_path):
    admin = MemoryAdmin()
    srv = make_server(tmp_path, port=0, admin=True, admin_svc=admin, admin_auth=MemoryAuth(ADMIN_USERS))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        _, headers, _ = post(srv, "/admin/api/login", {"email": "mina@keepframe.app", "password": "dev-admin"})
        cookie = headers.get("Set-Cookie").split(";")[0]
        status, _, suspended = post(srv, "/admin/api/tenants/org_hanbit/suspend", {}, cookie)
        assert status == 200 and suspended["tenant"]["status"] == "suspended"
        req = Request(f"http://127.0.0.1:{srv.server_address[1]}/admin/api/audit")
        req.add_header("Cookie", cookie)
        with urlopen(req) as response:
            events = json.loads(response.read())["events"]
        assert events[0]["action"] == "테넌트 정지" and events[0]["actor"] == "mina@keepframe.app"
        req = Request(f"http://127.0.0.1:{srv.server_address[1]}/admin/api/audit.csv")
        req.add_header("Cookie", cookie)
        with urlopen(req) as response:
            csv_body = response.read().decode()
            assert response.headers.get_content_type() == "text/csv"
        assert csv_body.splitlines()[0] == "ts,actor,action,target,detail"
        assert csv_body.splitlines()[1].split(",")[2] == "테넌트 정지"
    finally:
        srv.shutdown()
        srv.server_close()


def test_unsupported_audit_delete_and_policy_mutation_have_no_effect(tmp_path):
    admin = MemoryAdmin()
    srv = make_server(tmp_path, port=0, admin=True, admin_svc=admin, admin_auth=MemoryAuth(ADMIN_USERS))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        _, headers, _ = post(srv, "/admin/api/login", {"email": "mina@keepframe.app", "password": "dev-admin"})
        cookie = headers.get("Set-Cookie").split(";")[0]
        before = len(admin.list_audit())
        req = Request(f"http://127.0.0.1:{srv.server_address[1]}/admin/api/audit", method="DELETE", headers={"Cookie": cookie})
        try:
            urlopen(req)
            assert False, "audit DELETE unexpectedly succeeded"
        except HTTPError as error:
            assert error.code == 404
        for method in ("PUT", "PATCH"):
            req = Request(f"http://127.0.0.1:{srv.server_address[1]}/admin/api/policy", data=b"{}", method=method, headers={"Cookie": cookie})
            try:
                urlopen(req)
                assert False, f"policy {method} unexpectedly succeeded"
            except HTTPError as error:
                assert error.code == 404
        assert len(admin.list_audit()) == before
    finally:
        srv.shutdown()
        srv.server_close()

def test_login_page_links_back_to_landing_without_seed_password(tmp_path):
    srv = start_admin(tmp_path)
    try:
        req = Request(f"http://127.0.0.1:{srv.server_address[1]}/admin/login")
        with urlopen(req) as response:
            html = response.read().decode()
        assert 'href="/"' in html
        assert "dev-admin" not in html
        assert "mina@keepframe.app" not in html
    finally:
        srv.shutdown()
        srv.server_close()
