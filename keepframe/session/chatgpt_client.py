from __future__ import annotations

import http.client
import json
import platform
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .. import __version__
from .chatgpt_oauth import account_id_from_token, refresh_chatgpt_token
from .llm import AssistantReply
from .provider import ProviderConfig, load_llm_settings, save_llm_settings

BASE_URL = "https://chatgpt.com/backend-api/codex"
_REFRESH_LOCK = threading.Lock()
_TOKEN_FIELDS = ("api_key", "refresh_token", "id_token", "account_id", "oauth_expires_at")


def persist_refreshed_tokens(workspace: Path, refreshed: ProviderConfig) -> None:
    # Called by _refresh while holding _REFRESH_LOCK.
    saved = load_llm_settings(workspace) or refreshed.model_copy(deep=True)
    for field in _TOKEN_FIELDS:
        setattr(saved, field, getattr(refreshed, field))
    save_llm_settings(workspace, saved)


class ChatGPTClient:
    """ChatGPT account Responses transport; completed SSE items become agent replies."""

    supports_vision = True

    def __init__(
        self, config: ProviderConfig, base_url: str | None = None, timeout: float = 120.0,
        on_refresh: Callable[[ProviderConfig], None] | None = None,
        load_config: Callable[[], ProviderConfig | None] | None = None,
    ) -> None:
        self.config = config.model_copy(deep=True)
        self.base_url = (base_url or BASE_URL).rstrip("/")
        self.on_refresh = on_refresh
        self.load_config = load_config
        self.timeout = timeout
        self.session_id = str(uuid4())
        self._secrets: set[str] = set()
        self._remember_secrets()

    def _remember_secrets(self) -> None:
        self._secrets.update(value for value in (
            self.config.api_key, self.config.refresh_token, self.config.id_token, self.config.account_id,
        ) if value)

    def _error(self, context: str, detail: Any = "") -> RuntimeError:
        text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, "[redacted]")
        return RuntimeError(f"{context}: {text[:200]}" if text else context)

    def _refresh(self, rejected_access: str | None = None) -> None:
        with _REFRESH_LOCK:
            try:
                saved = self.load_config() if self.load_config is not None else None
                cfg = self.config
                if saved is not None and saved.provider == "chatgpt" and (
                    not cfg.account_id or not saved.account_id or cfg.account_id == saved.account_id
                ):
                    for field in _TOKEN_FIELDS:
                        setattr(cfg, field, getattr(saved, field))
                    self._remember_secrets()
                expired = cfg.oauth_expires_at <= time.time() + 60
                if cfg.api_key and not expired and cfg.api_key != rejected_access:
                    return
                if not cfg.refresh_token:
                    raise self._error("ChatGPT OAuth 토큰이 없거나 만료되었습니다. 다시 로그인하세요.")
                tokens = refresh_chatgpt_token(cfg.refresh_token)
                access = str(tokens.get("access_token") or "")
                if not access:
                    raise ValueError("access_token이 없습니다.")
                expires_in = int(tokens.get("expires_in") or 3600)
                cfg.api_key = access
                cfg.refresh_token = str(tokens.get("refresh_token") or cfg.refresh_token)
                cfg.id_token = str(tokens.get("id_token") or cfg.id_token)
                cfg.account_id = account_id_from_token(cfg.id_token or access) or cfg.account_id
                cfg.oauth_expires_at = time.time() + max(60, expires_in)
                self._remember_secrets()
                if self.on_refresh is not None:
                    self.on_refresh(cfg.model_copy(deep=True))
            except Exception as e:  # noqa: BLE001 - refresh errors may contain credentials
                raise self._error("ChatGPT OAuth 갱신 실패", str(e)) from None

    def _body(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        instructions = []
        items = []
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            if role == "system":
                instructions.append(content or "")
                continue
            if role == "tool":
                items.append({"type": "function_call_output", "call_id": message.get("tool_call_id"), "output": content or ""})
                continue
            parts = []
            text_type = "output_text" if role == "assistant" else "input_text"
            if isinstance(content, str) and content:
                parts.append({"type": text_type, "text": content})
            elif isinstance(content, list):
                for part in content:
                    if part.get("type") == "text":
                        parts.append({"type": text_type, "text": part.get("text", "")})
                    elif part.get("type") == "image_url":
                        image = part.get("image_url") or {}
                        converted = {"type": "input_image", "image_url": image.get("url") if isinstance(image, dict) else image}
                        if isinstance(image, dict) and "detail" in image:
                            converted["detail"] = image["detail"]
                        parts.append(converted)
            if parts:
                items.append({"type": "message", "role": role, "content": parts})
            if role == "assistant":
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    arguments = function.get("arguments") or "{}"
                    items.append({
                        "type": "function_call", "call_id": call.get("id"), "name": function.get("name"),
                        "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False),
                    })
        body = {
            "model": self.config.model, "instructions": "\n".join(instructions), "input": items,
            "stream": True, "store": False, "parallel_tool_calls": False,
            "reasoning": {"effort": self.config.extra.get("reasoning_effort", "low"), "summary": "auto"},
        }
        if tools:
            body["tools"] = [{
                "type": "function", "name": tool["function"]["name"],
                "description": tool["function"].get("description", ""),
                "parameters": tool["function"].get("parameters", {}), "strict": False,
            } for tool in tools]
            body["tool_choice"] = "auto"
        return body

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AssistantReply:
        self._refresh()
        for attempt in range(2):
            access = self.config.api_key
            try:
                return self._request(messages, tools, access)
            except urllib.error.HTTPError as e:
                try:
                    if e.code == 401 and attempt == 0:
                        self._refresh(rejected_access=access)
                        continue
                    detail = e.read(64 * 1024).decode("utf-8", errors="replace")
                except http.client.IncompleteRead as read_error:
                    raise self._error("ChatGPT 연결 실패", str(read_error)) from None
                finally:
                    e.close()
                raise self._error(f"ChatGPT 요청 실패({e.code})", detail) from None

    def _request(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], access: str) -> AssistantReply:
        req = urllib.request.Request(
            f"{self.base_url}/responses", data=json.dumps(self._body(messages, tools), ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json", "Authorization": f"Bearer {access}",
                "chatgpt-account-id": self.config.account_id, "OpenAI-Beta": "responses=experimental",
                "originator": "codex_cli_rs", "session_id": self.session_id, "Accept": "text/event-stream",
                "User-Agent": f"codex_cli_rs/{__version__} ({platform.system()}; {platform.machine()}) Keepframe",
            }, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                return self._read_stream(response)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError, http.client.IncompleteRead) as e:
            raise self._error("ChatGPT 연결 실패", str(e)) from None

    def _read_stream(self, response) -> AssistantReply:
        reply = AssistantReply()
        for line in response:
            line = line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if raw == "[DONE]":
                break
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                raise self._error("ChatGPT 스트림에 잘못된 JSON이 있습니다.") from None
            if not isinstance(event, dict):
                raise self._error("ChatGPT 스트림 이벤트 형식이 잘못되었습니다.") from None
            if "response" in event and not isinstance(event["response"], dict):
                raise self._error("ChatGPT response 형식이 잘못되었습니다.") from None
            kind = event.get("type")
            if kind in {"response.failed", "error", "response.incomplete"}:
                detail = event.get("response") or event
                raise self._error("ChatGPT 응답 실패", detail.get("error") or detail.get("incomplete_details") or detail) from None
            if kind == "response.completed":
                result = event.get("response") or {}
                if result.get("error") or result.get("status") in {"failed", "incomplete"}:
                    raise self._error("ChatGPT 응답 실패", result.get("error") or result) from None
                reply.content = reply.content.strip()
                return reply
            if kind != "response.output_item.done":
                continue
            item = event.get("item") or {}
            if item.get("type") == "message":
                reply.content += "".join(part.get("text", "") for part in item.get("content") or [] if part.get("type") == "output_text")
            elif item.get("type") == "function_call":
                try:
                    arguments = json.loads(item.get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    raise self._error("ChatGPT 도구 호출 arguments JSON이 잘못되었습니다.") from None
                if not isinstance(arguments, dict):
                    raise self._error("ChatGPT 도구 호출 arguments는 객체여야 합니다.") from None
                reply.tool_calls.append({"id": item.get("call_id"), "name": item.get("name"), "arguments": arguments})
        raise self._error("ChatGPT 스트림이 response.completed 전에 종료되었습니다.") from None
