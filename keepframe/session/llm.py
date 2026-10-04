from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..log import get
from .oauth import fetch_access_token
from .provider import OPENAI_COMPATIBLE_FALLBACK, ProviderConfig, default_base_url

log = get("keepframe.session")


@dataclass
class AssistantReply:
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


class LLMClient(Protocol):
    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AssistantReply: ...


class NullClient:
    """No LLM configured. Keeps the server alive and the tool registry testable."""

    supports_vision = False

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AssistantReply:
        return AssistantReply(
            content="에이전트가 설정되지 않았습니다. 관리자 LLM 페이지에 API 키를 저장하거나 KEEPFRAME_LLM_API_KEY(또는 OPENAI_API_KEY)를 넣으세요."
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
        override = os.environ.get("KEEPFRAME_LLM_VISION")
        markers = ("gpt-4o", "gpt-4.1", "gpt-5", "gpt-6", "vision", "llava", "gemini", "claude-3", "claude-4")
        self.supports_vision = override == "1" or (override != "0" and any(marker in self.model.lower() for marker in markers))

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AssistantReply:
        body = {
            "model": self.model,
            "messages": messages,
        }
        if tools:
            body.update(tools=tools, tool_choice="auto")
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
        override = config.extra.get("vision") if config.extra else None
        markers = ("gpt-4o", "gpt-4.1", "gpt-5", "gpt-6", "vision", "llava", "gemini", "claude-3", "claude-4")
        self.supports_vision = bool(override) if override is not None else any(marker in config.model.lower() for marker in markers)

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AssistantReply:
        kwargs: dict[str, Any] = {
            "model": self.config.litellm_model,
            "messages": messages,
        }
        if tools:
            kwargs.update(tools=tools, tool_choice="auto")
        if self.config.api_key and self.config.provider != "chatgpt":
            kwargs["api_key"] = self.config.api_key
        if self.config.base_url:
            kwargs["api_base"] = self.config.base_url
        oauth = self.config.oauth_params()
        if oauth.present() and self.config.provider != "chatgpt":
            token = fetch_access_token(oauth, provider=self.config.provider)
            if self.config.provider == "azure":
                kwargs["azure_ad_token"] = token
            else:
                kwargs["api_key"] = token
        kwargs.update(self.config.litellm_extra())
        kwargs.setdefault("timeout", 120)
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


def _openai_compatible_fallback(cfg: ProviderConfig) -> OpenAICompatibleClient | None:
    base = (cfg.base_url or default_base_url(cfg.provider) or "").rstrip("/")
    if cfg.provider in OPENAI_COMPATIBLE_FALLBACK or base.endswith("/v1"):
        return OpenAICompatibleClient(
            base_url=base or None,
            api_key=cfg.api_key or None,
            model=cfg.model or None,
        )
    return None


def make_llm(config: ProviderConfig | None = None, workspace: Path | None = None) -> LLMClient:
    cfg = config if config is not None else ProviderConfig.from_env()
    if not cfg.credentials_present():
        log.warning("no LLM credentials for provider=%s; session agent falls back to NullClient", cfg.provider)
        return NullClient()
    if cfg.provider == "chatgpt":
        from .chatgpt_client import ChatGPTClient, persist_refreshed_tokens
        from .provider import load_llm_settings
        return ChatGPTClient(
            cfg,
            on_refresh=(lambda refreshed: persist_refreshed_tokens(workspace, refreshed)) if workspace is not None else None,
            load_config=(lambda: load_llm_settings(workspace)) if workspace is not None else None,
        )
    try:
        return LiteLLMClient(cfg)
    except Exception as e:  # noqa: BLE001 - litellm missing or broken
        fallback = _openai_compatible_fallback(cfg)
        if fallback is not None:
            log.warning("litellm unavailable (%s); session agent uses OpenAI-compatible fallback", e)
            return fallback
        log.warning("litellm unavailable (%s); session agent falls back to NullClient", e)
        return NullClient()


def vision_llm(workspace: Path | None = None) -> LLMClient | None:
    """The configured LLM when it can see images, else None (captions are optional)."""
    from .provider import load_llm_settings
    saved = load_llm_settings(workspace)
    llm = make_llm(saved, workspace) if saved is not None else make_llm()
    return None if isinstance(llm, NullClient) or not getattr(llm, "supports_vision", False) else llm
