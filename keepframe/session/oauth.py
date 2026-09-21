from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from ..log import get

log = get("keepframe.session")

AZURE_SCOPE = "https://cognitiveservices.azure.com/.default"
OAUTH_KEYS = (
    "client_id",
    "client_secret",
    "tenant_id",
    "token_url",
    "scope",
    "azure_scope",
    "azure_username",
    "azure_password",
)


@dataclass
class OAuthParams:
    client_id: str = ""
    client_secret: str = ""
    tenant_id: str = ""
    token_url: str = ""
    scope: str = ""

    def present(self) -> bool:
        if not (self.client_id and self.client_secret):
            return False
        return bool(self.token_url or self.tenant_id)


def azure_token_url(tenant_id: str) -> str:
    return f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"


def resolve_token_url(params: OAuthParams) -> str:
    if params.token_url:
        return params.token_url
    if params.tenant_id:
        return azure_token_url(params.tenant_id)
    return ""


def resolve_scope(params: OAuthParams, provider: str = "") -> str:
    if params.scope:
        return params.scope
    if provider == "azure" or params.tenant_id:
        return AZURE_SCOPE
    return ""


_cache: dict[tuple[str, str, str], tuple[str, float]] = {}


def fetch_access_token(params: OAuthParams, provider: str = "", timeout: float = 30.0) -> str:
    """OAuth2 client_credentials grant. Caches until shortly before expiry."""
    token_url = resolve_token_url(params)
    if not token_url or not params.client_id or not params.client_secret:
        raise RuntimeError("OAuth client_id, client_secret, token_url 또는 tenant_id가 필요합니다.")
    scope = resolve_scope(params, provider)
    key = (token_url, params.client_id, scope)
    hit = _cache.get(key)
    if hit and hit[1] > time.time() + 60:
        return hit[0]
    token, expires_in = _request_token(token_url, params.client_id, params.client_secret, scope, timeout)
    _cache[key] = (token, time.time() + max(60, expires_in))
    return token


def clear_token_cache() -> None:
    _cache.clear()


def _request_token(token_url: str, client_id: str, client_secret: str, scope: str, timeout: float) -> tuple[str, int]:
    body = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    if scope:
        body["scope"] = scope
    req = urllib.request.Request(
        token_url,
        data=urllib.parse.urlencode(body).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        log.error("oauth token http %s: %s", e.code, detail[:400])
        raise RuntimeError(f"OAuth 토큰 요청 실패({e.code})") from e
    token = payload.get("access_token")
    if not token:
        raise RuntimeError("OAuth 응답에 access_token이 없습니다.")
    expires_in = int(payload.get("expires_in") or 3600)
    return str(token), expires_in
