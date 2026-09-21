from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..log import get
from .provider import ProviderConfig

log = get("keepframe.session")


@dataclass
class AssistantReply:
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


class LLMClient(Protocol):
    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AssistantReply: ...


class NullClient:
    """No LLM configured. Keeps the server alive and the tool registry testable."""

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AssistantReply:
        return AssistantReply(
            content="세션 에이전트가 설정되지 않았습니다. KEEPFRAME_LLM_API_KEY(또는 OPENAI_API_KEY)를 지정하세요."
        )


class OpenAICompatibleClient:
    """Minimal OpenAI-compatible chat completions client (stdlib urllib, tool calling)."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = (
            base_url
            or os.environ.get("KEEPFRAME_LLM_BASE_URL")
            or "https://api.openai.com/v1"
        ).rstrip("/")
        self.api_key = api_key or os.environ.get("KEEPFRAME_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
        self.model = model or os.environ.get("KEEPFRAME_LLM_MODEL") or "gpt-4o-mini"
        self.timeout = timeout

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AssistantReply:
        body = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            log.error("llm http %s: %s", e.code, detail[:400])
            raise RuntimeError(f"LLM 요청 실패({e.code})") from e

        msg = (payload.get("choices") or [{}])[0].get("message") or {}
        reply = AssistantReply(content=(msg.get("content") or "").strip())
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            reply.tool_calls.append({"id": tc.get("id"), "name": fn.get("name"), "arguments": args})
        return reply


class LiteLLMClient:
    """Multi-provider client: one interface for 100+ providers via LiteLLM."""

    def __init__(self, config: ProviderConfig) -> None:
        import litellm  # fail fast so make_llm can fall back

        self._litellm = litellm
        self.config = config

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AssistantReply:
        kwargs: dict[str, Any] = {
            "model": self.config.litellm_model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
        }
        if self.config.api_key:
            kwargs["api_key"] = self.config.api_key
        if self.config.base_url:
            kwargs["api_base"] = self.config.base_url
        kwargs.update(self.config.extra or {})
        resp = self._litellm.completion(**kwargs)
        msg = (resp.choices[0].message) if resp and resp.choices else None
        reply = AssistantReply()
        if msg is not None:
            reply.content = (getattr(msg, "content", None) or "").strip() or ""
            for tc in getattr(msg, "tool_calls", None) or []:
                fn = getattr(tc, "function", None) or (tc.get("function") if isinstance(tc, dict) else None)
                if fn is None:
                    continue
                name = getattr(fn, "name", None) or (fn.get("name") if isinstance(fn, dict) else None)
                raw_args = getattr(fn, "arguments", None) or (fn.get("arguments") if isinstance(fn, dict) else None) or "{}"
                args = {}
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args or {})
                except json.JSONDecodeError:
                    args = {}
                reply.tool_calls.append({"id": getattr(tc, "id", None), "name": name, "arguments": args})
        return reply


def make_llm(config: ProviderConfig | None = None) -> LLMClient:
    cfg = config if config is not None else ProviderConfig.from_env()
    if not cfg.credentials_present():
        log.warning("no LLM credentials for provider=%s; session agent falls back to NullClient", cfg.provider)
        return NullClient()
    try:
        return LiteLLMClient(cfg)
    except Exception as e:  # noqa: BLE001 - litellm missing or broken
        log.warning("litellm unavailable (%s); session agent falls back to NullClient", e)
        return NullClient()
