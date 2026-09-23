"""Strict output schemas for LLM tasks.

Each task has a Pydantic model (validation) and a matching hand-written JSON schema
(sent to the model for constrained decoding). The JSON schema is inlined (no $ref)
because local runtimes support refs unevenly. A test keeps the two in sync.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.categories import CategoryConfig, Priority


class RedFlag(StrEnum):
    """Warning signs the model reports separately from the category.

    Measured on the held-out set: a model can be talked into the injected category while
    still noticing the injection. Asking for red flags as a separate field catches that.
    """

    INSTRUCTIONS_TO_AI = "instructions_to_ai"
    CREDENTIAL_REQUEST = "credential_request"
    PAYMENT_DETAIL_CHANGE = "payment_detail_change"
    PRESSURE_OR_THREAT = "pressure_or_threat"


class ClassificationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str = Field(min_length=1, max_length=40)
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=600)
    priority: Priority
    requires_action: bool
    red_flags: list[RedFlag] = Field(max_length=len(RedFlag))


def classification_json_schema(categories: CategoryConfig) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": categories.names},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {"type": "string", "maxLength": 600},
            "priority": {"type": "string", "enum": [p.value for p in Priority]},
            "requires_action": {"type": "boolean"},
            "red_flags": {
                "type": "array",
                "items": {"type": "string", "enum": [f.value for f in RedFlag]},
                "uniqueItems": True,
                "maxItems": len(RedFlag),
            },
        },
        "required": [
            "category",
            "confidence",
            "reason",
            "priority",
            "requires_action",
            "red_flags",
        ],
        "additionalProperties": False,
    }
