from __future__ import annotations

import json
import platform
import time
import urllib.error
import urllib.request
from typing import Any
from uuid import uuid4

from .. import __version__
from .chatgpt_oauth import account_id_from_token, refresh_chatgpt_token
from .llm import AssistantReply
from .provider import ProviderConfig

BASE_URL = "https://chatgpt.com/backend-api/codex"


class ChatGPTClient:
    """ChatGPT account Responses transport; completed SSE items become agent replies."""

    supports_vision = True

    def __init__(self, config: ProviderConfig, base_url: str | None = None, timeout: float = 120.0) -> None:
        self.config = config.model_copy(deep=True)
        self.base_url = (base_url or config.base_url or BASE_URL).rstrip("/")
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

    def _refresh(self) -> None:
        cfg = self.config
        expired = bool(cfg.oauth_expires_at and cfg.oauth_expires_at <= time.time())
        if not cfg.api_key or expired:
            if not cfg.refresh_token:
                raise self._error("ChatGPT OAuth 토큰이 없거나 만료되었습니다. 다시 로그인하세요.") from None
            try:
                tokens = refresh_chatgpt_token(cfg.refresh_token)
                access = str(tokens.get("access_token") or "")
                if not access:
                    raise ValueError("access_token이 없습니다.")
                expires_in = int(tokens.get("expires_in") or 3600)
            except Exception as e:  # noqa: BLE001 - refresh errors may contain credentials
                raise self._error("ChatGPT OAuth 갱신 실패", str(e)) from None
            cfg.api_key = access
            cfg.refresh_token = str(tokens.get("refresh_token") or cfg.refresh_token)
            cfg.id_token = str(tokens.get("id_token") or cfg.id_token)
            cfg.account_id = account_id_from_token(cfg.id_token or access) or cfg.account_id
            cfg.oauth_expires_at = time.time() + max(60, expires_in)
            self._remember_secrets()

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
                        parts.append({"type": "input_image", "image_url": image.get("url") if isinstance(image, dict) else image})
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
            "include": ["reasoning.encrypted_content"], "reasoning": {"effort": "low", "summary": "auto"},
        }
        if tools:
            body["tools"] = [{
                "type": "function", "name": tool["function"]["name"],
                "description": tool["function"].get("description", ""),
                "parameters": tool["function"].get("parameters", {}),
            } for tool in tools]
            body["tool_choice"] = "auto"
        return body

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AssistantReply:
        self._refresh()
        req = urllib.request.Request(
            f"{self.base_url}/responses", data=json.dumps(self._body(messages, tools), ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json", "Authorization": f"Bearer {self.config.api_key}",
                "chatgpt-account-id": self.config.account_id, "OpenAI-Beta": "responses=experimental",
                "originator": "codex_cli_rs", "session_id": self.session_id, "Accept": "text/event-stream",
                "User-Agent": f"codex_cli_rs/{__version__} ({platform.system()}; {platform.machine()}) Keepframe",
            }, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                return self._read_stream(response)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise self._error(f"ChatGPT 요청 실패({e.code})", detail) from None
        except (urllib.error.URLError, OSError) as e:
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
