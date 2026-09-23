"""Provider-neutral LLM interface.

Providers only move text: they send a system + user message with a JSON schema and
return the raw text. Parsing and validation happen in `structured.py`, the same for every
provider. Provider errors are mapped to PipelineError codes here, and error details never
include prompts, responses or API keys.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.errors import ErrorCode, PipelineError


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    duration_ms: int | None = None


class LLMProvider(ABC):
    name: str
    model: str

    @abstractmethod
    def generate_json(
        self, *, system: str, user: str, schema: dict[str, Any], schema_name: str
    ) -> LLMResponse:
        """Return raw model text that should be JSON matching `schema`."""


def map_http_error(exc: Exception, provider: str) -> PipelineError:
    """Translate transport/HTTP failures into pipeline errors (no payloads in detail)."""
    if isinstance(exc, httpx.TimeoutException):
        return PipelineError(ErrorCode.LLM_TIMEOUT, f"{provider}: request timed out")
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            hint = "authentication failed (check API key)"
        elif code == 404:
            hint = "model or endpoint not found (is the model pulled?)"
        else:
            hint = f"HTTP {code}"
        return PipelineError(ErrorCode.LLM_UNAVAILABLE, f"{provider}: {hint}")
    if isinstance(exc, httpx.TransportError):
        return PipelineError(
            ErrorCode.LLM_UNAVAILABLE, f"{provider}: cannot connect ({type(exc).__name__})"
        )
    return PipelineError(ErrorCode.LLM_UNAVAILABLE, f"{provider}: {type(exc).__name__}")
