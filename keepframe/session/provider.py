from __future__ import annotations

import os

from typing import Literal

from pydantic import BaseModel, Field

from .oauth import OAuthParams, OAUTH_KEYS

# Providers that do not need an API key (local runtimes).
LOCAL_PROVIDERS = {"ollama", "vllm", "lm_studio", "local", "llamacpp", "huggingface", "openai_compatible"}

# Admin LLM picker. `id` is the LiteLLM prefix except openai_compatible → openai/.
PROVIDER_CATALOG: tuple[dict, ...] = (
    {"id": "chatgpt", "label": "ChatGPT", "default_model": "gpt-5.4", "default_base_url": "", "auth_modes": ["oauth"], "live_models": False, "hide_base_url": True, "hide_api_key": True},
    {"id": "openai", "label": "OpenAI", "default_model": "gpt-4o-mini", "default_base_url": "https://api.openai.com/v1", "auth_modes": ["api_key"], "live_models": True},
    {"id": "openai_compatible", "label": "OpenAI Compatible", "default_model": "", "default_base_url": "http://127.0.0.1:8000/v1", "auth_modes": ["api_key"], "live_models": True},
    {"id": "anthropic", "label": "Anthropic Claude", "default_model": "claude-sonnet-4-5", "default_base_url": "https://api.anthropic.com", "auth_modes": ["api_key"], "live_models": True},
    {"id": "gemini", "label": "Google Gemini", "default_model": "gemini-2.5-flash", "default_base_url": "https://generativelanguage.googleapis.com/v1beta", "auth_modes": ["api_key"], "live_models": True},
    {"id": "vertex_ai", "label": "Google Vertex AI", "default_model": "gemini-2.5-flash", "default_base_url": "", "auth_modes": ["api_key"], "live_models": False, "hide_base_url": True},
    {"id": "azure", "label": "Azure OpenAI", "default_model": "gpt-4o", "default_base_url": "", "auth_modes": ["api_key", "oauth"], "live_models": True},
    {"id": "bedrock", "label": "AWS Bedrock", "default_model": "anthropic.claude-3-5-sonnet-20241022-v2:0", "default_base_url": "", "auth_modes": ["api_key"], "live_models": False, "hide_base_url": True},
    {"id": "groq", "label": "Groq", "default_model": "llama-3.3-70b-versatile", "default_base_url": "https://api.groq.com/openai/v1", "auth_modes": ["api_key"], "live_models": True},
    {"id": "mistral", "label": "Mistral", "default_model": "mistral-small-latest", "default_base_url": "https://api.mistral.ai/v1", "auth_modes": ["api_key"], "live_models": True},
    {"id": "cohere", "label": "Cohere", "default_model": "command-r-plus", "default_base_url": "https://api.cohere.ai/v1", "auth_modes": ["api_key"], "live_models": True},
    {"id": "deepseek", "label": "DeepSeek", "default_model": "deepseek-chat", "default_base_url": "https://api.deepseek.com", "auth_modes": ["api_key"], "live_models": True},
    {"id": "openrouter", "label": "OpenRouter", "default_model": "openai/gpt-4o-mini", "default_base_url": "https://openrouter.ai/api/v1", "auth_modes": ["api_key"], "live_models": True},
    {"id": "together_ai", "label": "Together AI", "default_model": "meta-llama/Llama-3.3-70B-Instruct-Turbo", "default_base_url": "https://api.together.xyz/v1", "auth_modes": ["api_key"], "live_models": True},
    {"id": "fireworks_ai", "label": "Fireworks AI", "default_model": "accounts/fireworks/models/llama-v3p1-70b-instruct", "default_base_url": "https://api.fireworks.ai/inference/v1", "auth_modes": ["api_key"], "live_models": True},
    {"id": "xai", "label": "xAI Grok", "default_model": "grok-3", "default_base_url": "https://api.x.ai/v1", "auth_modes": ["api_key"], "live_models": True},
    {"id": "perplexity", "label": "Perplexity", "default_model": "sonar", "default_base_url": "https://api.perplexity.ai", "auth_modes": ["api_key"], "live_models": True},
    {"id": "ollama", "label": "Ollama (로컬)", "default_model": "llama3.2", "default_base_url": "http://127.0.0.1:11434", "auth_modes": ["api_key"], "live_models": True, "hide_api_key": True},
    {"id": "vllm", "label": "vLLM (로컬)", "default_model": "", "default_base_url": "http://127.0.0.1:8000/v1", "auth_modes": ["api_key"], "live_models": True},
    {"id": "lm_studio", "label": "LM Studio (로컬)", "default_model": "", "default_base_url": "http://127.0.0.1:1234/v1", "auth_modes": ["api_key"], "live_models": True},
)


def catalog_entry(provider: str) -> dict:
    for row in PROVIDER_CATALOG:
        if row["id"] == provider:
            return row
    return {}


def default_base_url(provider: str) -> str:
    return str(catalog_entry(provider).get("default_base_url") or "")

# Canonical env var each provider reads when no explicit api_key is given.
PROVIDER_ENV_KEYS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "google": "GEMINI_API_KEY",
    "vertex_ai": "GOOGLE_APPLICATION_CREDENTIALS",
    "groq": "GROQ_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "cohere": "COHERE_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "azure": "AZURE_API_KEY",
    "bedrock": "AWS_ACCESS_KEY_ID",
    "together_ai": "TOGETHERAI_API_KEY",
    "fireworks_ai": "FIREWORKS_AI_API_KEY",
    "xai": "XAI_API_KEY",
    "perplexity": "PERPLEXITYAI_API_KEY",
}


class ProviderConfig(BaseModel):
    """A LiteLLM-compatible provider selection. ``provider`` is the LiteLLM prefix."""

    provider: str = "openai"
    model: str = "gpt-4o-mini"
    auth: Literal["api_key", "oauth"] = "api_key"
    api_key: str = ""
    base_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    tenant_id: str = ""
    token_url: str = ""
    scope: str = ""
    refresh_token: str = ""
    id_token: str = ""
    account_id: str = ""
    oauth_expires_at: float = 0.0
    extra: dict = Field(default_factory=dict)

    @property
    def litellm_model(self) -> str:
        if self.provider == "openai_compatible":
            return f"openai/{self.model}"
        return f"{self.provider}/{self.model}"

    def chatgpt_connected(self) -> bool:
        return self.provider == "chatgpt" and self.auth == "oauth" and bool(self.refresh_token or self.api_key)

    def public_dump(self) -> dict:
        d = self.model_dump()
        d["chatgpt_connected"] = self.chatgpt_connected()
        if self.chatgpt_connected():
            d["api_key"] = ""
            d["refresh_token"] = "***" if self.refresh_token else ""
            d["id_token"] = ""
        return d

    def oauth_params(self) -> OAuthParams:
        extra = self.extra or {}
        return OAuthParams(
            client_id=self.client_id or str(extra.get("client_id") or ""),
            client_secret=self.client_secret or str(extra.get("client_secret") or ""),
            tenant_id=self.tenant_id or str(extra.get("tenant_id") or ""),
            token_url=self.token_url or str(extra.get("token_url") or ""),
            scope=self.scope or str(extra.get("scope") or extra.get("azure_scope") or ""),
        )

    def litellm_extra(self) -> dict:
        return {k: v for k, v in (self.extra or {}).items() if k not in OAUTH_KEYS}

    def credentials_present(self) -> bool:
        if self.chatgpt_connected():
            return True
        if self.oauth_params().present():
            return True
        if self.api_key or self.base_url:
            return True
        if self.provider == "openai_compatible" and self.base_url:
            return True
        if self.provider in LOCAL_PROVIDERS:
            return True
        return bool(os.environ.get(PROVIDER_ENV_KEYS.get(self.provider, "")))

    @classmethod
    def from_env(cls) -> "ProviderConfig":
        provider = os.environ.get("KEEPFRAME_LLM_PROVIDER") or "openai"
        model = os.environ.get("KEEPFRAME_LLM_MODEL") or "gpt-4o-mini"
        base_url = os.environ.get("KEEPFRAME_LLM_BASE_URL") or ""
        api_key = (
            os.environ.get("KEEPFRAME_LLM_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get(PROVIDER_ENV_KEYS.get(provider, ""))
            or ""
        )
        return cls(provider=provider, model=model, api_key=api_key, base_url=base_url)
