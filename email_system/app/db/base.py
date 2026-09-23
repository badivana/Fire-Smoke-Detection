"""Declarative base, shared column types and enums.

Enums are stored as VARCHAR + CHECK constraint (not native DB enums) so SQLite and
PostgreSQL behave the same and adding a value is a plain migration.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, MetaData, TypeDecorator
from sqlalchemy.orm import DeclarativeBase

# Stable constraint names so Alembic can alter/drop them (SQLite batch mode needs this).
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Always stores UTC and always returns tz-aware UTC (SQLite drops tzinfo)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime not allowed; use timezone-aware UTC")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class EmailStatus(StrEnum):
    NEW = "NEW"
    CLASSIFIED = "CLASSIFIED"
    DRAFT_GENERATED = "DRAFT_GENERATED"
    UNDER_REVIEW = "UNDER_REVIEW"
    APPROVED = "APPROVED"
    SENT = "SENT"
    REJECTED = "REJECTED"
    EDIT_REQUIRED = "EDIT_REQUIRED"
    FAILED = "FAILED"
    ERROR = "ERROR"


class EmailSource(StrEnum):
    DEMO = "demo"
    GMAIL = "gmail"


class DraftSource(StrEnum):
    AI = "ai"
    ADMIN_EDIT = "admin_edit"


class ApprovalDecision(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EDIT_REQUIRED = "EDIT_REQUIRED"


class TextSource(StrEnum):
    TEXT_LAYER = "text_layer"
    OCR = "ocr"
    NONE = "none"


def str_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )
