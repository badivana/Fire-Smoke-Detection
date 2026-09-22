"""Loads and validates config/categories.yaml.

The file is validated strictly at startup: a typo in the config must stop the app,
not silently mis-route emails.
"""

from __future__ import annotations

import re
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.config import get_settings

_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,39}$")


class ExtractionSchema(StrEnum):
    REQUIREMENT = "requirement"
    QUOTATION = "quotation"
    INVOICE = "invoice"
    NONE = "none"


class Priority(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    URGENT = "URGENT"


class CategoryDef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str = Field(min_length=10, max_length=500)
    extraction_schema: ExtractionSchema = ExtractionSchema.NONE
    draft_reply: bool = True
    default_priority: Priority = Priority.MEDIUM
    requires_action: bool = True

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(f"category name {v!r} must be UPPER_SNAKE_CASE (2-40 chars)")
        return v


class SpamSignals(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    gmail_labels: tuple[str, ...] = ("SPAM",)
    bulk_headers: tuple[str, ...] = ()
    precedence_values: tuple[str, ...] = ()
    trusted_sender_domains: tuple[str, ...] = ()
    suspicious_phrases: tuple[str, ...] = ()

    @field_validator("trusted_sender_domains", "suspicious_phrases", "precedence_values")
    @classmethod
    def _lower(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(s.strip().lower() for s in v if s.strip())


class CategoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fallback_category: str
    categories: tuple[CategoryDef, ...] = Field(min_length=2)
    spam_signals: SpamSignals = SpamSignals()

    @model_validator(mode="after")
    def _check(self) -> CategoryConfig:
        names = [c.name for c in self.categories]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate category names: {sorted(dupes)}")
        if self.fallback_category not in names:
            raise ValueError(f"fallback_category {self.fallback_category!r} is not defined")
        # There must be a category where replies are never auto-drafted (spam bucket),
        # and the fallback must not be it (unknown != spam).
        if all(c.draft_reply for c in self.categories):
            raise ValueError("at least one category must have draft_reply: false (spam bucket)")
        if not self.get(self.fallback_category).draft_reply:
            raise ValueError("fallback_category must not be a no-draft (spam) category")
        return self

    @property
    def names(self) -> list[str]:
        return [c.name for c in self.categories]

    def get(self, name: str) -> CategoryDef:
        for c in self.categories:
            if c.name == name:
                return c
        raise KeyError(name)

    def resolve(self, name: str | None) -> tuple[CategoryDef, bool]:
        """Map an LLM-returned name to a category. Returns (category, was_unknown)."""
        norm = (name or "").strip().upper()
        if norm in self.names:
            return self.get(norm), False
        return self.get(self.fallback_category), True


def load_categories(path: Path) -> CategoryConfig:
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return CategoryConfig.model_validate(raw)


@lru_cache
def get_categories() -> CategoryConfig:
    return load_categories(get_settings().categories_file)
