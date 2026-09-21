import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from keepframe.admin.auth import MemoryAuth
from keepframe.admin.memory import MemoryAdmin
from keepframe.session.provider import ProviderConfig
from keepframe.web.server import make_server
import threading


def start_admin(tmp_path):
    srv = make_server(tmp_path, port=0, admin=True, admin_svc=MemoryAdmin(), admin_auth=MemoryAuth())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _login(srv):
    req = Request(
        f"http://127.0.0.1:{srv.server_address[1]}/admin/api/login",
        data=json.dumps({"email": "mina@keepframe.app", "password": "dev-admin"}).encode(),
        method="POST",
        headers={"content-type": "application/json"},
    )
    with urlopen(req) as r:
        return r.headers.get("Set-Cookie").split(";")[0]


def _api(srv, path, cookie, method="GET", body=None):
    req = Request(f"http://127.0.0.1:{srv.server_address[1]}{path}", method=method)
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("content-type", "application/json")
    req.add_header("Cookie", cookie)
    try:
        with urlopen(req) as r:
            return r.status, json.loads(r.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


def test_memory_admin_llm_settings():
    admin = MemoryAdmin()
    assert admin.get_llm_settings().provider == "openai"
    admin.set_llm_settings(ProviderConfig(provider="anthropic", model="claude-3-5-sonnet", api_key="k"), "mina@keepframe.app")
    assert admin.get_llm_settings().provider == "anthropic"
    assert admin.list_audit()[0].action == "LLM 프로바이더 변경"


def test_admin_llm_api_get_and_post(tmp_path):
    srv = start_admin(tmp_path)
    try:
        cookie = _login(srv)
        code, body = _api(srv, "/admin/api/llm", cookie)
        assert code == 200
        assert body["settings"]["provider"] == "openai"

        code, body = _api(
            srv,
            "/admin/api/llm",
            cookie,
            method="POST",
            body={"settings": {"provider": "anthropic", "model": "claude-3-5-sonnet", "api_key": "sk-x"}},
        )
        assert code == 200
        assert body["settings"]["provider"] == "anthropic"

        code, body = _api(srv, "/admin/api/llm", cookie)
        assert body["settings"]["model"] == "claude-3-5-sonnet"
    finally:
        srv.shutdown()


def test_admin_llm_page_requires_auth(tmp_path):
    srv = start_admin(tmp_path)
    try:
        code, body = _api(srv, "/admin/api/llm", "keepframe_admin=bad")
        assert code == 401
    finally:
        srv.shutdown()
