"""OpenAI-compatible provider (/v1/chat/completions), e.g. NVIDIA NIM, vLLM, LM Studio."""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.core.errors import ErrorCode, PipelineError
from app.llm.base import LLMProvider, LLMResponse, map_http_error


class OpenAICompatibleProvider(LLMProvider):
    name = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None,
        timeout: float,
        temperature: float = 0.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model = model
        self.temperature = temperature
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout, headers=headers, transport=transport
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
            "temperature": self.temperature,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema, "strict": True},
            },
        }
        start = time.monotonic()
        try:
            resp = self._client.post("/chat/completions", json=payload)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise map_http_error(exc, self.name) from None
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            content = None
        if not isinstance(content, str):
            raise PipelineError(
                ErrorCode.LLM_SCHEMA_INVALID, "openai_compatible: response had no content"
            )
        return LLMResponse(
            text=content,
            model=str(data.get("model") or self.model),
            duration_ms=int((time.monotonic() - start) * 1000),
        )
