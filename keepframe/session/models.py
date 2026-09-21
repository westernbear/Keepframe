from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .. import __version__
from .provider import PROVIDER_CATALOG, catalog_entry, default_base_url

CHATGPT_MODELS = ("gpt-5.4", "gpt-5", "gpt-5-mini", "gpt-4.1", "gpt-4o")
USER_AGENT = f"Keepframe/{__version__}"
_OPENER = urllib.request.build_opener()
_OPENER.addheaders = []


class ModelListError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _request_headers(extra: dict | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if extra:
        headers.update({k: v for k, v in extra.items() if v})
    return headers


def _http_error_text(code: int, detail: str) -> str:
    low = (detail or "").lower()
    if "error 1010" in low or "cloudflare" in low:
        return (
            f"모델 목록이 거부되었습니다({code}). "
            "엔드포인트가 이 서버의 요청을 차단했습니다. OpenAI Compatible Base URL과 API 키를 확인하세요."
        )
    try:
        payload = json.loads(detail)
        msg = payload.get("error") or payload.get("message") or payload.get("title") or payload.get("detail")
        if isinstance(msg, dict):
            msg = msg.get("message") or msg.get("code")
        if msg:
            return f"모델 목록 요청 실패({code}): {str(msg)[:180]}"
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass
    stripped = " ".join((detail or "").split())
    if not stripped or stripped.startswith("<"):
        return f"모델 목록 요청 실패({code})"
    return f"모델 목록 요청 실패({code}): {stripped[:180]}"


def openai_models_url(base: str) -> str:
    b = (base or "").rstrip("/")
    if b.endswith("/v1"):
        return f"{b}/models"
    return f"{b}/v1/models"


def parse_openai_models(payload: dict) -> list[str]:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    ids = [str(item.get("id")) for item in rows if isinstance(item, dict) and item.get("id")]
    return sorted(set(ids))


def parse_ollama_models(payload: dict) -> list[str]:
    rows = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    names = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("model")
        if name:
            names.append(str(name))
    return sorted(set(names))


def parse_gemini_models(payload: dict) -> list[str]:
    rows = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    names = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        raw = str(item.get("name") or "")
        if raw.startswith("models/"):
            raw = raw[len("models/") :]
        if raw:
            names.append(raw)
    return sorted(set(names))


def list_models(provider: str, base_url: str = "", api_key: str = "", timeout: float = 15.0) -> tuple[list[str], str]:
    """Return (model ids, source). source is static, live, or none."""
    entry = catalog_entry(provider)
    if provider == "chatgpt":
        return list(CHATGPT_MODELS), "static"
    if not entry.get("live_models"):
        fallback = entry.get("default_model")
        return ([str(fallback)] if fallback else []), "static"
    needs_key = provider not in {"ollama", "vllm", "lm_studio", "openai_compatible"}
    if needs_key and not api_key:
        fallback = entry.get("default_model")
        return ([str(fallback)] if fallback else []), "static"
    base = (base_url or default_base_url(provider)).rstrip("/")
    if provider == "ollama":
        payload = _json_get(f"{base or 'http://127.0.0.1:11434'}/api/tags", timeout=timeout)
        return parse_ollama_models(payload), "live"
    if not base:
        raise RuntimeError("모델을 불러오려면 Base URL이 필요합니다.")
    if provider == "anthropic":
        payload = _json_get(
            f"{base}/v1/models" if not base.endswith("/v1") else f"{base}/models",
            timeout=timeout,
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        )
        return parse_openai_models(payload), "live"
    if provider == "gemini":
        q = urllib.parse.urlencode({"key": api_key}) if api_key else ""
        url = f"{base}/models" + (f"?{q}" if q else "")
        return parse_gemini_models(_json_get(url, timeout=timeout)), "live"
    payload = _openai_models(base, api_key, timeout)
    return parse_openai_models(payload), "live"


def _openai_models(base: str, api_key: str, timeout: float) -> dict:
    headers = _request_headers({"Authorization": f"Bearer {api_key}"} if api_key else None)
    url = openai_models_url(base)
    try:
        return _json_get(url, timeout=timeout, headers=headers)
    except ModelListError as e:
        if e.status in (401, 403):
            raise
        alt = f"{base.rstrip('/')}/models"
        if alt == url:
            raise
        return _json_get(alt, timeout=timeout, headers=headers)


def _json_get(url: str, timeout: float, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, headers=_request_headers(headers), method="GET")
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise ModelListError(_http_error_text(e.code, detail), e.code) from e
    except urllib.error.URLError as e:
        raise ModelListError(f"모델 목록에 연결하지 못했습니다: {e.reason}") from e
    if isinstance(payload, dict):
        return payload
    return {}


def catalog_public() -> list[dict]:
    return [dict(row) for row in PROVIDER_CATALOG]
