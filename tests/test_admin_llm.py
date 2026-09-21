import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from keepframe.admin.auth import MemoryAuth
from keepframe.admin.memory import MemoryAdmin
from keepframe.session.provider import ProviderConfig
from keepframe.web.server import make_server
import threading


def start_admin(tmp_path):
    admin = MemoryAdmin()
    srv = make_server(tmp_path, port=0, admin=True, admin_svc=admin, admin_auth=MemoryAuth())
    srv.admin_svc = admin
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


def test_memory_admin_llm_settings_persist(tmp_path):
    admin = MemoryAdmin(workspace=tmp_path)
    admin.set_llm_settings(
        ProviderConfig(provider="openai", model="gpt-4o-mini", api_key="sk-live"),
        "mina@keepframe.app",
    )
    path = tmp_path / "admin" / "llm.json"
    assert path.is_file()
    reloaded = MemoryAdmin(workspace=tmp_path, seed=False)
    assert reloaded.get_llm_settings().api_key == "sk-live"
    assert reloaded.get_llm_settings().model == "gpt-4o-mini"


def test_admin_llm_api_get_and_post(tmp_path):
    srv = start_admin(tmp_path)
    try:
        cookie = _login(srv)
        code, body = _api(srv, "/admin/api/llm", cookie)
        assert code == 200
        assert body["settings"]["provider"] == "openai"
        assert any(p["id"] == "openai_compatible" for p in body["catalog"])
        assert any(p["id"] == "chatgpt" for p in body["catalog"])

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

        code, body = _api(
            srv,
            "/admin/api/llm",
            cookie,
            method="POST",
            body={
                "settings": {
                    "provider": "azure",
                    "model": "gpt-4o",
                    "auth": "oauth",
                    "client_id": "app",
                    "client_secret": "secret",
                    "tenant_id": "tid",
                    "scope": "https://cognitiveservices.azure.com/.default",
                }
            },
        )
        assert code == 200
        assert body["settings"]["auth"] == "oauth"
        assert body["settings"]["client_id"] == "app"
        assert body["settings"]["tenant_id"] == "tid"
        assert body["settings"]["chatgpt_connected"] is False
    finally:
        srv.shutdown()


def test_admin_chatgpt_oauth_start_and_disconnect(tmp_path, monkeypatch):
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(tmp_path))
    monkeypatch.setattr("keepframe.session.chatgpt_oauth.ChatGPTOAuth.ensure_listener", lambda self: None)
    srv = start_admin(tmp_path)
    try:
        cookie = _login(srv)
        code, body = _api(srv, "/admin/api/llm/oauth/start?provider=chatgpt", cookie)
        assert code == 200
        assert "client_id=app_EMoamEEZ73f0CkXaXp7hrann" in body["url"]
        assert "redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback" in body["url"]

        code, body = _api(srv, "/admin/api/llm/oauth/start?provider=azure", cookie)
        assert code == 400

        code, body = _api(srv, "/admin/api/llm/oauth/start?provider=chatgpt", "keepframe_admin=bad")
        assert code == 401

        from keepframe.session.provider import ProviderConfig

        srv.admin_svc.set_llm_settings(
            ProviderConfig(provider="chatgpt", auth="oauth", api_key="at", refresh_token="rt", model="gpt-5.4"),
            "mina@keepframe.app",
        )
        (tmp_path / "auth.json").write_text("{}", encoding="utf-8")
        code, body = _api(srv, "/admin/api/llm/oauth/disconnect", cookie, method="POST")
        assert code == 200
        assert body["settings"]["chatgpt_connected"] is False
        assert body["settings"]["provider"] == "openai"
        assert not (tmp_path / "auth.json").exists()
    finally:
        srv.shutdown()


def test_admin_llm_save_keeps_chatgpt_tokens(tmp_path):
    srv = start_admin(tmp_path)
    try:
        cookie = _login(srv)
        from keepframe.session.provider import ProviderConfig

        srv.admin_svc.set_llm_settings(
            ProviderConfig(provider="chatgpt", auth="oauth", api_key="at", refresh_token="rt", model="gpt-5.4"),
            "mina@keepframe.app",
        )
        code, body = _api(
            srv,
            "/admin/api/llm",
            cookie,
            method="POST",
            body={"settings": {"provider": "chatgpt", "model": "gpt-5.4", "auth": "oauth"}},
        )
        assert code == 200
        assert body["settings"]["chatgpt_connected"] is True
        assert srv.admin_svc.get_llm_settings().refresh_token == "rt"
        assert body["settings"]["refresh_token"] == "***"
    finally:
        srv.shutdown()


def test_admin_llm_page_requires_auth(tmp_path):
    srv = start_admin(tmp_path)
    try:
        code, body = _api(srv, "/admin/api/llm", "keepframe_admin=bad")
        assert code == 401
    finally:
        srv.shutdown()


def test_admin_llm_models_chatgpt_and_compatible(tmp_path, monkeypatch):
    srv = start_admin(tmp_path)
    try:
        cookie = _login(srv)
        code, body = _api(srv, "/admin/api/llm/models", cookie, method="POST", body={"provider": "chatgpt"})
        assert code == 200
        assert body["source"] == "static"
        assert "gpt-5.4" in body["models"]

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"data": [{"id": "local-llama"}, {"id": "local-qwen"}]}).encode()

        monkeypatch.setattr("keepframe.session.models._OPENER.open", lambda req, data=None, timeout=None: _Resp())
        code, body = _api(
            srv,
            "/admin/api/llm/models",
            cookie,
            method="POST",
            body={"provider": "openai_compatible", "base_url": "http://127.0.0.1:8000/v1", "api_key": "x"},
        )
        assert code == 200
        assert body["source"] == "live"
        assert body["models"] == ["local-llama", "local-qwen"]
    finally:
        srv.shutdown()
