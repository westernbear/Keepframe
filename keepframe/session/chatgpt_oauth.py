from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..log import get
from .provider import ProviderConfig

log = get("keepframe.session")

CHATGPT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CHATGPT_ISSUER = "https://auth.openai.com"
CHATGPT_CALLBACK_PORT = 1455
CHATGPT_REDIRECT_URI = f"http://localhost:{CHATGPT_CALLBACK_PORT}/auth/callback"
CHATGPT_SCOPE = "openid profile email offline_access"
CHATGPT_DEFAULT_MODEL = "gpt-5.4"
OPENAI_AUTH_CLAIM = "https://api.openai.com/auth"


@dataclass
class PendingLogin:
    state: str
    verifier: str
    actor: str
    origin: str
    created: float = field(default_factory=time.time)


def generate_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def authorize_url(state: str, challenge: str, originator: str = "keepframe") -> str:
    q = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": CHATGPT_CLIENT_ID,
            "redirect_uri": CHATGPT_REDIRECT_URI,
            "scope": CHATGPT_SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "state": state,
            "originator": originator,
        }
    )
    return f"{CHATGPT_ISSUER}/oauth/authorize?{q}"


def safe_origin(origin: str) -> str:
    u = urlparse(origin)
    host = (u.hostname or "").lower()
    if u.scheme != "http" or host not in ("127.0.0.1", "localhost"):
        return "http://127.0.0.1:8765"
    return origin.rstrip("/")


def callback_bind_host(serve_host: str | None = None) -> str:
    env = (os.getenv("CHATGPT_CALLBACK_BIND") or "").strip()
    if env:
        return env
    if serve_host and serve_host not in ("127.0.0.1", "localhost", "::1"):
        return "0.0.0.0"
    return "127.0.0.1"


def chatgpt_auth_file() -> Path:
    token_dir = os.getenv("CHATGPT_TOKEN_DIR") or os.path.expanduser("~/.config/litellm/chatgpt")
    name = os.getenv("CHATGPT_AUTH_FILE") or "auth.json"
    return Path(token_dir) / name


def decode_jwt_claims(token: str) -> dict:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    pad = "=" * (-len(parts[1]) % 4)
    try:
        raw = base64.urlsafe_b64decode(parts[1] + pad)
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def account_id_from_token(token: str) -> str:
    claims = decode_jwt_claims(token)
    auth = claims.get(OPENAI_AUTH_CLAIM)
    if isinstance(auth, dict):
        aid = auth.get("chatgpt_account_id")
        if isinstance(aid, str) and aid:
            return aid
    return ""


def exchange_code(code: str, verifier: str, timeout: float = 30.0) -> dict:
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CHATGPT_REDIRECT_URI,
            "client_id": CHATGPT_CLIENT_ID,
            "code_verifier": verifier,
        }
    )
    return _post_token(body, timeout)


def refresh_chatgpt_token(refresh_token: str, timeout: float = 30.0) -> dict:
    payload = json.dumps(
        {
            "client_id": CHATGPT_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": "openid profile email",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{CHATGPT_ISSUER}/oauth/token",
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    return _read_token_response(req, timeout)


def _post_token(body: str, timeout: float) -> dict:
    req = urllib.request.Request(
        f"{CHATGPT_ISSUER}/oauth/token",
        data=body.encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    return _read_token_response(req, timeout)


def _read_token_response(req: urllib.request.Request, timeout: float) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        log.error("chatgpt oauth http %s: %s", e.code, detail[:400])
        raise RuntimeError(f"ChatGPT OAuth 실패({e.code})") from e
    if not payload.get("access_token"):
        raise RuntimeError("ChatGPT OAuth 응답에 access_token이 없습니다.")
    return payload


def write_litellm_auth(tokens: dict) -> Path:
    access = str(tokens.get("access_token") or "")
    ident = str(tokens.get("id_token") or access)
    expires_in = int(tokens.get("expires_in") or 3600)
    record = {
        "access_token": access,
        "refresh_token": tokens.get("refresh_token") or "",
        "id_token": tokens.get("id_token") or "",
        "expires_at": time.time() + max(60, expires_in),
        "account_id": account_id_from_token(ident),
    }
    path = chatgpt_auth_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def clear_litellm_auth() -> None:
    path = chatgpt_auth_file()
    if path.is_file():
        path.unlink()


def preserve_chatgpt_tokens(incoming: ProviderConfig, previous: ProviderConfig) -> ProviderConfig:
    if incoming.provider != "chatgpt" or incoming.auth != "oauth":
        return incoming
    if previous.provider != "chatgpt" or previous.auth != "oauth":
        return incoming
    data = incoming.model_dump()
    for key in ("api_key", "refresh_token", "id_token", "account_id"):
        if not data.get(key) and getattr(previous, key, ""):
            data[key] = getattr(previous, key)
    if not data.get("oauth_expires_at") and previous.oauth_expires_at:
        data["oauth_expires_at"] = previous.oauth_expires_at
    return ProviderConfig.model_validate(data)


def tokens_to_config(tokens: dict, previous: ProviderConfig | None = None) -> ProviderConfig:
    access = str(tokens.get("access_token") or "")
    ident = str(tokens.get("id_token") or access)
    expires_in = int(tokens.get("expires_in") or 3600)
    model = previous.model if previous and previous.provider == "chatgpt" else CHATGPT_DEFAULT_MODEL
    return ProviderConfig(
        provider="chatgpt",
        model=model or CHATGPT_DEFAULT_MODEL,
        auth="oauth",
        api_key=access,
        refresh_token=str(tokens.get("refresh_token") or ""),
        id_token=str(tokens.get("id_token") or ""),
        account_id=account_id_from_token(ident),
        oauth_expires_at=time.time() + max(60, expires_in),
        extra=(previous.extra if previous else {}) or {},
    )


class ChatGPTOAuth:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, PendingLogin] = {}
        self._admin_svc = None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.bind_host = callback_bind_host()
        self.workspace: Path | None = None

    def _pending_path(self) -> Path | None:
        if self.workspace is None:
            return None
        return Path(self.workspace) / ".chatgpt-oauth-pending.json"

    def _store_pending(self, pending: PendingLogin) -> None:
        self._pending = {pending.state: pending}
        path = self._pending_path()
        if path is None:
            return
        path.write_text(
            json.dumps(
                {
                    "state": pending.state,
                    "verifier": pending.verifier,
                    "actor": pending.actor,
                    "origin": pending.origin,
                    "created": pending.created,
                }
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)

    def _load_pending(self, state: str) -> PendingLogin | None:
        hit = self._pending.pop(state, None)
        if hit is not None:
            path = self._pending_path()
            if path is not None and path.is_file():
                path.unlink()
            return hit
        path = self._pending_path()
        if path is None or not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if str(data.get("state") or "") != state:
            return None
        path.unlink()
        return PendingLogin(
            state=str(data.get("state") or ""),
            verifier=str(data.get("verifier") or ""),
            actor=str(data.get("actor") or ""),
            origin=str(data.get("origin") or ""),
            created=float(data.get("created") or 0),
        )

    def begin(self, actor: str, origin: str, admin_svc, listen: bool = True) -> str:
        verifier, challenge = generate_pkce()
        state = secrets.token_urlsafe(24)
        pending = PendingLogin(state=state, verifier=verifier, actor=actor, origin=safe_origin(origin))
        with self._lock:
            self._admin_svc = admin_svc
            self._store_pending(pending)
        if listen:
            self.ensure_listener()
        return authorize_url(state, challenge)

    def complete(self, code: str, state: str) -> ProviderConfig:
        with self._lock:
            pending = self._load_pending(state)
            admin_svc = self._admin_svc
        if pending is None or time.time() - pending.created > 600:
            raise RuntimeError("OAuth state가 만료되었거나 올바르지 않습니다.")
        tokens = exchange_code(code, pending.verifier)
        write_litellm_auth(tokens)
        previous = admin_svc.get_llm_settings() if admin_svc is not None else None
        config = tokens_to_config(tokens, previous)
        if admin_svc is not None:
            admin_svc.set_llm_settings(config, pending.actor)
        return config

    def origin_for(self, state: str) -> str:
        with self._lock:
            pending = self._pending.get(state)
            if pending is None:
                path = self._pending_path()
                if path is not None and path.is_file():
                    try:
                        data = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        data = {}
                    if str(data.get("state") or "") == state:
                        return str(data.get("origin") or "http://127.0.0.1:8765")
        return pending.origin if pending else "http://127.0.0.1:8765"

    def ensure_listener(self) -> None:
        with self._lock:
            if self._server is not None:
                return
            host = self.bind_host or callback_bind_host()
            try:
                server = ThreadingHTTPServer((host, CHATGPT_CALLBACK_PORT), _CallbackHandler)
            except OSError as e:
                raise RuntimeError(
                    f"ChatGPT 콜백 포트 {CHATGPT_CALLBACK_PORT}를 열 수 없습니다. Codex 등 다른 프로그램이 쓰고 있으면 종료하세요."
                ) from e
            server.oauth = self  # type: ignore[attr-defined]
            self._server = server
            thread = threading.Thread(target=server.serve_forever, daemon=True, name="chatgpt-oauth")
            self._thread = thread
            thread.start()
            log.info("chatgpt oauth callback http://localhost:%s/auth/callback bind=%s", CHATGPT_CALLBACK_PORT, host)


FLOW = ChatGPTOAuth()


_SUCCESS = """<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"/><title>Keepframe</title></head>
<body><p>ChatGPT 연결이 완료되었습니다.</p><p><a href="{href}">관리자로 돌아가기</a></p>
<script>location.replace("{href}")</script></body></html>"""

_ERROR = """<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"/><title>Keepframe</title></head>
<body><p>ChatGPT 연결에 실패했습니다.</p><p>{msg}</p><p><a href="{href}">관리자로 돌아가기</a></p></body></html>"""


class _CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info("chatgpt oauth %s", fmt % args)

    def do_GET(self):
        oauth: ChatGPTOAuth = self.server.oauth  # type: ignore[attr-defined]
        u = urlparse(self.path)
        if u.path.rstrip("/") != "/auth/callback":
            self._html(404, _ERROR.format(msg="not found", href="http://127.0.0.1:8765/admin/llm"))
            return
        q = parse_qs(u.query)
        state = (q.get("state") or [""])[0]
        origin = oauth.origin_for(state)
        ok_href = html.escape(f"{origin}/admin/llm?oauth=ok", quote=True)
        err_href = html.escape(f"{origin}/admin/llm?oauth=err", quote=True)
        err = (q.get("error_description") or q.get("error") or [""])[0]
        if err:
            self._html(400, _ERROR.format(msg="authorization denied", href=err_href))
            return
        code = (q.get("code") or [""])[0]
        if not code or not state:
            self._html(400, _ERROR.format(msg="missing code", href=err_href))
            return
        try:
            oauth.complete(code, state)
        except Exception as e:  # noqa: BLE001
            log.error("chatgpt oauth complete failed: %s", e)
            self._html(400, _ERROR.format(msg="token exchange failed", href=err_href))
            return
        self._html(200, _SUCCESS.format(href=ok_href))

    def _html(self, code: int, body: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(raw)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(raw)
