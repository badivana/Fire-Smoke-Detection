"""Source-independent ingestion input. Demo samples and (Phase 11) Gmail both map to this."""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.db.base import EmailSource


class IncomingAttachment(BaseModel):
    model_config = ConfigDict(frozen=True)

    filename: str = Field(max_length=1024)
    mime_type: str = Field(default="application/octet-stream", max_length=255)
    data: bytes


class IncomingEmail(BaseModel):
    model_config = ConfigDict(frozen=True)

    message_id: str = Field(min_length=1, max_length=255)
    thread_id: str | None = Field(default=None, max_length=255)
    source: EmailSource
    sender: str = Field(max_length=1024)  # raw "Name <addr>" as received
    to: list[str] = Field(default_factory=list)
    subject: str = ""
    body_text: str | None = None
    body_html: str | None = None
    received_at: AwareDatetime  # naive datetimes are rejected
    headers: dict[str, str] = Field(default_factory=dict)
    labels: list[str] = Field(default_factory=list)
    attachments: list[IncomingAttachment] = Field(default_factory=list)


class IngestResult(BaseModel):
    email_id: int
    created: bool
    duplicate: bool
    spam_signals: list[str]
    review_reasons: list[str]
    needs_manual_review: bool
