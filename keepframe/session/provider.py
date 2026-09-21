from __future__ import annotations

import os

from pydantic import BaseModel, Field

# Providers that do not need an API key (local runtimes).
LOCAL_PROVIDERS = {"ollama", "vllm", "lm_studio", "local", "llamacpp", "huggingface"}

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
    api_key: str = ""
    base_url: str = ""
    extra: dict = Field(default_factory=dict)

    @property
    def litellm_model(self) -> str:
        return f"{self.provider}/{self.model}"

    def credentials_present(self) -> bool:
        if self.api_key or self.base_url:
            return True
        if self.provider in LOCAL_PROVIDERS:
            return True
        if self.extra and any(k in self.extra for k in ("client_secret", "token_url", "client_id", "tenant_id")):
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
