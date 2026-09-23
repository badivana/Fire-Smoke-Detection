"""Structured LLM calls: JSON schema in, validated Pydantic model out.

Spec: validate with Pydantic; on failure retry ONCE with a stricter prompt; then fail with
LLM_SCHEMA_INVALID (-> ERROR state). Connection/timeout errors are not retried here:
they surface immediately and the admin uses /retry.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from app.core.errors import ErrorCode, PipelineError
from app.llm.base import LLMProvider

T = TypeVar("T", bound=BaseModel)

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)

STRICT_RETRY_SUFFIX = (
    "\n\nIMPORTANT: Your previous answer was rejected because it was not valid JSON "
    "matching the required schema ({errors}). Respond with ONLY one JSON object that "
    "matches the schema exactly: no prose, no markdown, no extra keys, all required keys "
    "present, values of the correct type."
)


@dataclass(frozen=True)
class StructuredResult(Generic[T]):  # noqa: UP046  (3.11 compatible)
    value: T
    attempts: int
    model: str
    duration_ms: int | None


def extract_json(text: str) -> Any:
    """Parse model output, tolerating <think> blocks, code fences and surrounding prose."""
    cleaned = _FENCE.sub("", _THINK.sub("", text).strip()).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(cleaned[start : end + 1])


def _summarise(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = [f"{'.'.join(map(str, e['loc'])) or 'root'}: {e['msg']}" for e in exc.errors()]
        return "; ".join(parts)[:400]
    return f"invalid JSON ({type(exc).__name__})"


def call_structured(
    provider: LLMProvider,
    *,
    system: str,
    user: str,
    output_model: type[T],
    schema: dict[str, Any],
    schema_name: str,
    max_retries: int,
) -> StructuredResult[T]:
    last_error = ""
    for attempt in range(1, max_retries + 2):
        sys_prompt = (
            system if attempt == 1 else system + STRICT_RETRY_SUFFIX.format(errors=last_error)
        )
        resp = provider.generate_json(
            system=sys_prompt, user=user, schema=schema, schema_name=schema_name
        )
        try:
            value = output_model.model_validate(extract_json(resp.text))
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = _summarise(exc)
            continue
        return StructuredResult(
            value=value, attempts=attempt, model=resp.model, duration_ms=resp.duration_ms
        )
    raise PipelineError(
        ErrorCode.LLM_SCHEMA_INVALID,
        f"{schema_name}: output invalid after {max_retries + 1} attempts: {last_error}",
    )
