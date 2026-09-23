"""ORM models for the 8 spec tables.

Delete rules: pipeline outputs (attachments, classifications, extractions, drafts)
cascade with their email. Approvals, audit logs and processing errors are evidence,
so deleting an email that has any of them is refused (RESTRICT).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import (
    ApprovalDecision,
    Base,
    DraftSource,
    EmailSource,
    EmailStatus,
    TextSource,
    UTCDateTime,
    str_enum,
    utcnow,
)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)


class Email(TimestampMixin, Base):
    __tablename__ = "emails"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Gmail message id (or demo-generated id). Unique => duplicate ingestion is impossible.
    message_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    thread_id: Mapped[str | None] = mapped_column(String(255), index=True)
    source: Mapped[EmailSource] = mapped_column(str_enum(EmailSource, "email_source"))
    sender: Mapped[str] = mapped_column(String(320), nullable=False)
    sender_name: Mapped[str | None] = mapped_column(String(255))
    recipients: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    subject: Mapped[str] = mapped_column(String(998), default="", nullable=False)
    body_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    status: Mapped[EmailStatus] = mapped_column(
        str_enum(EmailStatus, "email_status"), default=EmailStatus.NEW, nullable=False, index=True
    )
    # Denormalised from the latest classification for fast queue filtering.
    category: Mapped[str | None] = mapped_column(String(40), index=True)
    priority: Mapped[str | None] = mapped_column(String(16))

    needs_manual_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    review_reasons: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    spam_signals: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    processing_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(64))

    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    sent_provider_message_id: Mapped[str | None] = mapped_column(String(255))

    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, onupdate=utcnow, nullable=False
    )
    # Optimistic lock: two concurrent approve/send requests cannot both win.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    __mapper_args__ = {"version_id_col": version}

    __table_args__ = (
        CheckConstraint("processing_attempts >= 0", name="attempts_non_negative"),
        # A SENT email must record when it was sent.
        CheckConstraint("status != 'SENT' OR sent_at IS NOT NULL", name="sent_has_timestamp"),
    )

    attachments: Mapped[list[Attachment]] = relationship(
        back_populates="email", cascade="all, delete-orphan", passive_deletes=True
    )
    classifications: Mapped[list[Classification]] = relationship(
        back_populates="email",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Classification.id",
    )
    extractions: Mapped[list[Extraction]] = relationship(
        back_populates="email",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Extraction.id",
    )
    drafts: Mapped[list[Draft]] = relationship(
        back_populates="email",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Draft.version",
    )
    approvals: Mapped[list[Approval]] = relationship(
        back_populates="email", passive_deletes="all", order_by="Approval.id"
    )
    audit_logs: Mapped[list[AuditLog]] = relationship(
        back_populates="email", passive_deletes="all", order_by="AuditLog.id"
    )
    errors: Mapped[list[ProcessingError]] = relationship(
        back_populates="email", passive_deletes="all", order_by="ProcessingError.id"
    )

    @property
    def current_draft(self) -> Draft | None:
        current = [d for d in self.drafts if d.is_current]
        return current[-1] if current else None


class Attachment(TimestampMixin, Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int] = mapped_column(
        ForeignKey("emails.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(127), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_path: Mapped[str | None] = mapped_column(String(512))
    extracted_text: Mapped[str | None] = mapped_column(Text)
    text_source: Mapped[TextSource] = mapped_column(
        str_enum(TextSource, "text_source"), default=TextSource.NONE, nullable=False
    )
    page_count: Mapped[int | None] = mapped_column(Integer)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    parse_error: Mapped[str | None] = mapped_column(String(500))

    email: Mapped[Email] = relationship(back_populates="attachments")

    __table_args__ = (CheckConstraint("size_bytes >= 0", name="size_non_negative"),)


class Classification(TimestampMixin, Base):
    __tablename__ = "classifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int] = mapped_column(
        ForeignKey("emails.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    raw_category: Mapped[str | None] = mapped_column(String(100))  # exactly what the LLM said
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    priority: Mapped[str] = mapped_column(String(16), nullable=False)
    requires_action: Mapped[bool] = mapped_column(Boolean, nullable=False)
    unknown_category: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    low_confidence: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    llm_attempts: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    email: Mapped[Email] = relationship(back_populates="classifications")

    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    )


class Extraction(TimestampMixin, Base):
    __tablename__ = "extractions"

    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int] = mapped_column(
        ForeignKey("emails.id", ondelete="CASCADE"), nullable=False, index=True
    )
    schema_name: Mapped[str] = mapped_column(String(32), nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    missing_information: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)

    email: Mapped[Email] = relationship(back_populates="extractions")


class Draft(TimestampMixin, Base):
    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int] = mapped_column(
        ForeignKey("emails.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_draft_id: Mapped[int | None] = mapped_column(
        ForeignKey("drafts.id", ondelete="SET NULL")
    )
    source: Mapped[DraftSource] = mapped_column(str_enum(DraftSource, "draft_source"))
    subject: Mapped[str] = mapped_column(String(998), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    missing_information: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    reason_for_reply: Mapped[str] = mapped_column(Text, default="", nullable=False)
    requires_human_review: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(100))  # None for admin edits
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)

    email: Mapped[Email] = relationship(back_populates="drafts")

    __table_args__ = (
        UniqueConstraint("email_id", "version", name="uq_drafts_email_version"),
        # Spec: every draft requires human review. Enforced by the DB, not just code.
        CheckConstraint("requires_human_review", name="always_human_review"),
        CheckConstraint("version >= 1", name="version_positive"),
    )


class Approval(TimestampMixin, Base):
    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int] = mapped_column(
        ForeignKey("emails.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    draft_id: Mapped[int | None] = mapped_column(ForeignKey("drafts.id", ondelete="RESTRICT"))
    decision: Mapped[ApprovalDecision] = mapped_column(
        str_enum(ApprovalDecision, "approval_decision"), nullable=False
    )
    approver: Mapped[str] = mapped_column(String(100), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)

    email: Mapped[Email] = relationship(back_populates="approvals")

    __table_args__ = (
        # An approval must point at the exact draft text that was approved.
        CheckConstraint(
            "decision != 'APPROVED' OR draft_id IS NOT NULL", name="approved_has_draft"
        ),
        CheckConstraint("length(trim(approver)) > 0", name="approver_not_blank"),
    )


class AuditLog(TimestampMixin, Base):
    """Append-only. Never store full email bodies or secrets in `details`."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int | None] = mapped_column(
        ForeignKey("emails.id", ondelete="RESTRICT"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str | None] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(100))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    email: Mapped[Email | None] = relationship(back_populates="audit_logs")


class ProcessingError(TimestampMixin, Base):
    __tablename__ = "processing_errors"

    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int | None] = mapped_column(
        ForeignKey("emails.id", ondelete="RESTRICT"), index=True
    )
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    email: Mapped[Email | None] = relationship(back_populates="errors")


Index("ix_emails_status_received", Email.status, Email.received_at)
# At most one current draft per email (partial unique index; SQLite + PostgreSQL).
Index(
    "uq_drafts_one_current_per_email",
    Draft.email_id,
    unique=True,
    sqlite_where=text("is_current = 1"),
    postgresql_where=text("is_current"),
)
