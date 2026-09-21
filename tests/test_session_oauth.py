import json
from keepframe.session.oauth import (
    AZURE_SCOPE,
    OAuthParams,
    azure_token_url,
    clear_token_cache,
    fetch_access_token,
    resolve_scope,
    resolve_token_url,
)


def test_azure_token_url_and_scope():
    params = OAuthParams(client_id="id", client_secret="sec", tenant_id="tid")
    assert azure_token_url("tid") == "https://login.microsoftonline.com/tid/oauth2/v2.0/token"
    assert resolve_token_url(params).endswith("/tid/oauth2/v2.0/token")
    assert resolve_scope(params, "azure") == AZURE_SCOPE
    assert params.present() is True
    assert OAuthParams(client_id="id", client_secret="sec").present() is False


def test_fetch_access_token_client_credentials(monkeypatch):
    clear_token_cache()
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"access_token": "tok-1", "expires_in": 3600}).encode()

    def _fake(req, timeout=None):
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", _fake)
    params = OAuthParams(
        client_id="cid",
        client_secret="csec",
        token_url="https://idp.example/oauth2/token",
        scope="api/.default",
    )
    token = fetch_access_token(params)
    assert token == "tok-1"
    assert captured["url"] == "https://idp.example/oauth2/token"
    body = captured["data"].decode()
    assert "grant_type=client_credentials" in body
    assert "client_id=cid" in body
    assert "scope=api%2F.default" in body
    assert fetch_access_token(params) == "tok-1"
    assert isinstance(captured.get("timeout"), (int, float)) or captured.get("timeout") is None
