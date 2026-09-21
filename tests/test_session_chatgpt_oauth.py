import base64
import hashlib
import json
from urllib.parse import parse_qs, urlparse

from keepframe.admin.memory import MemoryAdmin
from keepframe.session.chatgpt_oauth import (
    CHATGPT_CLIENT_ID,
    CHATGPT_REDIRECT_URI,
    ChatGPTOAuth,
    account_id_from_token,
    authorize_url,
    callback_bind_host,
    generate_pkce,
    preserve_chatgpt_tokens,
    write_litellm_auth,
)
from keepframe.session.provider import ProviderConfig


def _jwt(payload: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"aaa.{raw}.sig"


def test_pkce_s256():
    verifier, challenge = generate_pkce()
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    assert challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    assert "=" not in challenge


def test_authorize_url_uses_codex_public_client():
    url = authorize_url("st", "ch")
    q = parse_qs(urlparse(url).query)
    assert url.startswith("https://auth.openai.com/oauth/authorize?")
    assert q["client_id"] == [CHATGPT_CLIENT_ID]
    assert q["redirect_uri"] == [CHATGPT_REDIRECT_URI]
    assert q["code_challenge_method"] == ["S256"]
    assert q["code_challenge"] == ["ch"]
    assert q["state"] == ["st"]


def test_account_id_from_id_token():
    token = _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acc-9"}})
    assert account_id_from_token(token) == "acc-9"


def test_write_litellm_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(tmp_path))
    path = write_litellm_auth({"access_token": "at", "refresh_token": "rt", "id_token": "", "expires_in": 120})
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["access_token"] == "at"
    assert data["refresh_token"] == "rt"
    assert data["expires_at"] > 0


def test_complete_writes_auth_and_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(tmp_path))
    admin = MemoryAdmin()
    flow = ChatGPTOAuth()
    url = flow.begin("mina@keepframe.app", "http://127.0.0.1:8765", admin, listen=False)
    state = parse_qs(urlparse(url).query)["state"][0]
    monkeypatch.setattr(
        "keepframe.session.chatgpt_oauth.exchange_code",
        lambda code, verifier: {
            "access_token": "at",
            "refresh_token": "rt",
            "id_token": _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acc-1"}}),
            "expires_in": 3600,
        },
    )
    cfg = flow.complete("the-code", state)
    assert cfg.provider == "chatgpt"
    assert cfg.auth == "oauth"
    assert cfg.model == "gpt-5.4"
    assert cfg.refresh_token == "rt"
    assert cfg.account_id == "acc-1"
    assert admin.get_llm_settings().refresh_token == "rt"
    dumped = admin.get_llm_settings().public_dump()
    assert dumped["chatgpt_connected"] is True
    assert dumped["api_key"] == ""
    assert dumped["refresh_token"] == "***"
    assert json.loads((tmp_path / "auth.json").read_text(encoding="utf-8"))["access_token"] == "at"


def test_preserve_chatgpt_tokens_on_save():
    prev = ProviderConfig(provider="chatgpt", auth="oauth", api_key="at", refresh_token="rt", model="gpt-5.4")
    incoming = ProviderConfig(provider="chatgpt", auth="oauth", model="gpt-5.4")
    kept = preserve_chatgpt_tokens(incoming, prev)
    assert kept.refresh_token == "rt"
    assert kept.api_key == "at"
    other = preserve_chatgpt_tokens(ProviderConfig(provider="azure", auth="oauth"), prev)
    assert other.refresh_token == ""


def test_callback_bind_host(monkeypatch):
    monkeypatch.delenv("CHATGPT_CALLBACK_BIND", raising=False)
    assert callback_bind_host("127.0.0.1") == "127.0.0.1"
    assert callback_bind_host("0.0.0.0") == "0.0.0.0"
    monkeypatch.setenv("CHATGPT_CALLBACK_BIND", "0.0.0.0")
    assert callback_bind_host("127.0.0.1") == "0.0.0.0"
