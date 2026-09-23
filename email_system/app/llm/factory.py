"""Build the configured provider."""

from __future__ import annotations

from app.core.config import LLMProviderName, Settings, get_settings
from app.llm.base import LLMProvider
from app.llm.ollama import OllamaProvider
from app.llm.openai_compat import OpenAICompatibleProvider


def build_provider(settings: Settings | None = None, *, model: str | None = None) -> LLMProvider:
    s = settings or get_settings()
    if s.llm_provider == LLMProviderName.OLLAMA:
        return OllamaProvider(
            base_url=s.ollama_base_url,
            model=model or s.ollama_model,
            timeout=s.llm_timeout_seconds,
            temperature=s.llm_temperature,
            think=s.llm_think,
        )
    key = s.openai_compat_api_key.get_secret_value() if s.openai_compat_api_key else None
    return OpenAICompatibleProvider(
        base_url=s.openai_compat_base_url or "",
        model=model or s.openai_compat_model or "",
        api_key=key,
        timeout=s.llm_timeout_seconds,
        temperature=s.llm_temperature,
    )
