"""Ollama provider using /api/chat with structured output (`format` = JSON schema)."""

from __future__ import annotations

from typing import Any

import httpx

from app.core.errors import ErrorCode, PipelineError
from app.llm.base import LLMProvider, LLMResponse, map_http_error


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout: float,
        temperature: float = 0.0,
        think: bool = False,
        num_ctx: int = 8192,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model = model
        self.temperature = temperature
        self.think = think
        self.num_ctx = num_ctx
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout, transport=transport
        )

    def generate_json(
        self, *, system: str, user: str, schema: dict[str, Any], schema_name: str
    ) -> LLMResponse:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "format": schema,  # constrained decoding to this JSON schema
            "stream": False,
            "think": self.think,  # qwen3: reasoning text off (breaks strict JSON otherwise)
            "options": {"temperature": self.temperature, "num_ctx": self.num_ctx},
        }
        try:
            resp = self._client.post("/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise map_http_error(exc, self.name) from None
        content = (data.get("message") or {}).get("content")
        if not isinstance(content, str):
            raise PipelineError(ErrorCode.LLM_SCHEMA_INVALID, "ollama: response had no content")
        duration = data.get("total_duration")
        return LLMResponse(
            text=content,
            model=str(data.get("model") or self.model),
            duration_ms=int(duration / 1e6) if isinstance(duration, int | float) else None,
        )
