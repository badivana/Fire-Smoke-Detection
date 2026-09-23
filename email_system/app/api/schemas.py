"""API request/response models. Email text is returned as plain strings; the dashboard
must render it as text, never as HTML."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class EmailSummary(_ORM):
    id: int
    sender: str
    sender_name: str | None
    subject: str
    category: str | None
    priority: str | None
    status: str
    received_at: datetime
    needs_manual_review: bool
    has_draft: bool = False


class EmailList(BaseModel):
    total: int
    items: list[EmailSummary]


class AttachmentOut(_ORM):
    id: int
    filename: str
    mime_type: str
    size_bytes: int
    text_source: str
    page_count: int | None
    truncated: bool
    parse_error: str | None


class ClassificationOut(_ORM):
    category: str
    raw_category: str | None
    confidence: float
    reason: str
    priority: str
    requires_action: bool
    unknown_category: bool
    low_confidence: bool
    red_flags: list[str]
    model_name: str
    prompt_version: str
    created_at: datetime


class ExtractionOut(_ORM):
    schema_name: str
    data: dict[str, Any]
    missing_information: list[str]
    ungrounded_fields: list[str]
    model_name: str
    prompt_version: str
    created_at: datetime


class DraftOut(_ORM):
    id: int
    version: int
    source: str
    subject: str
    body: str
    missing_information: list[str]
    reason_for_reply: str
    requires_human_review: bool
    warnings: list[str]
    model_name: str | None
    created_by: str
    created_at: datetime


class DraftVersion(_ORM):
    id: int
    version: int
    source: str
    created_by: str
    created_at: datetime
    is_current: bool


class ApprovalOut(_ORM):
    id: int
    draft_id: int | None
    decision: str
    approver: str
    comment: str | None
    created_at: datetime


class EmailDetail(EmailSummary):
    message_id: str
    thread_id: str | None
    recipients: list[str]
    body_text: str
    review_reasons: list[str]
    spam_signals: list[str]
    processing_attempts: int
    last_error_code: str | None
    send_started_at: datetime | None
    sent_at: datetime | None
    sent_provider_message_id: str | None
    attachments: list[AttachmentOut]
    classification: ClassificationOut | None
    extraction: ExtractionOut | None
    draft: DraftOut | None
    draft_versions: list[DraftVersion]
    approvals: list[ApprovalOut]
    allowed_actions: list[str]
    ai_banner: str = "AI GENERATED \u2014 HUMAN REVIEW REQUIRED"


class AuditOut(_ORM):
    id: int
    event_type: str
    from_status: str | None
    to_status: str | None
    actor: str
    model_name: str | None
    details: dict[str, Any]
    created_at: datetime


class DraftEdit(BaseModel):
    subject: str = Field(min_length=1, max_length=998)
    body: str = Field(min_length=1, max_length=20_000)
    expected_draft_id: int | None = None


class ApproveRequest(BaseModel):
    draft_id: int
    acknowledge_warnings: bool = False
    comment: str | None = Field(default=None, max_length=2000)


class RejectRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


class EditRequest(BaseModel):
    comment: str = Field(min_length=1, max_length=2000)


class RegenerateRequest(BaseModel):
    instructions: str | None = Field(default=None, max_length=1000)


class SendResult(BaseModel):
    email_id: int
    status: str
    provider_message_id: str


class Stats(BaseModel):
    total: int
    by_status: dict[str, int]
    by_category: dict[str, int]
    needs_manual_review: int
    awaiting_review: int
    errors: int
    sent: int
    send_mode: str
