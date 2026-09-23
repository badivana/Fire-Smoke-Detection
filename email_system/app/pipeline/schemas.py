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


# ---------------------------------------------------------------------------- extraction
# Amounts, IDs and dates are strings copied exactly as written ("INR 12,00,000"): the model
# must not calculate, convert or reformat. Unknown => null. Values are then checked
# against the source text by app/pipeline/grounding.py.

_S = 200  # max length for short text fields


class RequestType(StrEnum):
    PROCUREMENT = "procurement"
    SOFTWARE_LICENSE = "software_license"
    SERVICE = "service"
    REPAIR = "repair"
    OTHER = "other"


class RequirementItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=_S)
    quantity: int | None = Field(ge=0, le=1_000_000)


class RequirementExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    department: str | None = Field(max_length=_S)
    request_type: RequestType
    items: list[RequirementItem] = Field(max_length=50)
    budget: str | None = Field(max_length=_S)
    deadline: str | None = Field(max_length=_S)
    technical_specifications: str | None = Field(max_length=2000)
    missing_information: list[str] = Field(max_length=20)


class PricedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=_S)
    qty: int | None = Field(ge=0, le=1_000_000)
    unit_price: str | None = Field(max_length=_S)


class QuotationExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vendor: str | None = Field(max_length=_S)
    quotation_no: str | None = Field(max_length=_S)
    items: list[PricedItem] = Field(max_length=50)
    taxes: str | None = Field(max_length=_S)
    total: str | None = Field(max_length=_S)
    validity: str | None = Field(max_length=_S)
    delivery_terms: str | None = Field(max_length=_S)
    attachment_filename: str | None = Field(max_length=255)
    missing_information: list[str] = Field(max_length=20)


class InvoiceExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vendor: str | None = Field(max_length=_S)
    invoice_no: str | None = Field(max_length=_S)
    invoice_date: str | None = Field(max_length=_S)
    po_reference: str | None = Field(max_length=_S)
    items: list[PricedItem] = Field(max_length=50)
    taxes: str | None = Field(max_length=_S)
    total: str | None = Field(max_length=_S)
    due_date: str | None = Field(max_length=_S)
    missing_information: list[str] = Field(max_length=20)


EXTRACTION_MODELS: dict[str, type[BaseModel]] = {
    "requirement": RequirementExtraction,
    "quotation": QuotationExtraction,
    "invoice": InvoiceExtraction,
}


def inline_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic schema -> self-contained strict schema for constrained decoding.

    Resolves $ref, turns Optional[X] (anyOf X|null) into type [X, "null"], marks every
    property required and forbids extra keys (what OpenAI strict mode also requires).
    """
    raw = model.model_json_schema()
    defs = raw.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return walk(defs[node["$ref"].rsplit("/", 1)[-1]])
        if "anyOf" in node:
            options = [walk(o) for o in node["anyOf"]]
            non_null = [o for o in options if o.get("type") != "null"]
            if len(non_null) == 1 and len(options) == 2:
                merged = dict(non_null[0])
                merged["type"] = [merged["type"], "null"]
                return merged
            return {"anyOf": options}
        out = {k: walk(v) for k, v in node.items() if k not in ("title", "default", "$defs")}
        if out.get("type") == "object" and "properties" in out:
            out["required"] = list(out["properties"])
            out["additionalProperties"] = False
        return out

    return walk(raw)
