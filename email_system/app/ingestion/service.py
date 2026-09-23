"""Ingest one email: dedupe -> clean -> spam signals -> store -> audit. Never sends."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.categories import get_categories
from app.core.config import get_settings
from app.core.logging import EventType
from app.db.base import EmailStatus, TextSource
from app.db.models import Attachment, Email
from app.ingestion.models import IncomingEmail, IngestResult
from app.ingestion.normalize import clean_line, clean_text, html_to_text, truncate
from app.ingestion.spam_signals import (
    Signals,
    attachment_signals,
    content_signals,
    header_signals,
    parse_sender,
)
from app.ingestion.storage import sanitize_filename, sha256_hex, store_attachment
from app.services import audit

# Hard cap on what we keep in the DB (prompts are truncated further, per MAX_EMAIL_CHARS).
MAX_STORED_BODY_CHARS = 200_000


def _find(db: Session, message_id: str) -> Email | None:
    return db.scalar(select(Email).where(Email.message_id == message_id))


def _duplicate(db: Session, existing: Email, incoming: IncomingEmail) -> IngestResult:
    audit.record(
        db,
        EventType.DUPLICATE_IGNORED,
        email=existing,
        details={"message_id": incoming.message_id, "source": incoming.source.value},
    )
    db.commit()
    return IngestResult(
        email_id=existing.id,
        created=False,
        duplicate=True,
        spam_signals=existing.spam_signals,
        review_reasons=existing.review_reasons,
        needs_manual_review=existing.needs_manual_review,
    )


def ingest_email(db: Session, incoming: IncomingEmail) -> IngestResult:
    existing = _find(db, incoming.message_id)
    if existing is not None:
        return _duplicate(db, existing, incoming)

    settings = get_settings()
    cfg = get_categories().spam_signals
    sig = Signals()

    # --- body: prefer text/plain; always parse HTML so hidden text is detected ---
    hidden = ""
    raw_body = incoming.body_text or ""
    if incoming.body_html:
        html = html_to_text(incoming.body_html)
        hidden = html.hidden_text
        if not raw_body.strip():
            raw_body = html.text
    cleaned = clean_text(raw_body)
    body, cut = truncate(cleaned.text, MAX_STORED_BODY_CHARS)
    if cut:
        sig.add("body_truncated_on_store")
    subject = clean_line(incoming.subject, 998)

    sender = parse_sender(incoming.sender)
    header_signals(sig, cfg, sender, incoming.headers, incoming.labels)
    content_signals(
        sig,
        cfg,
        subject=subject,
        body=body,
        hidden_text=hidden,
        invisible_chars=cleaned.invisible_chars_removed,
    )

    email = Email(
        message_id=incoming.message_id,
        thread_id=incoming.thread_id,
        rfc_message_id=incoming.rfc_message_id,
        source=incoming.source,
        sender=sender.address[:320],
        sender_name=clean_line(sender.name, 255) if sender.name else None,
        recipients=[r[:320] for r in incoming.to][:50],
        subject=subject,
        body_text=body,
        received_at=incoming.received_at,
        status=EmailStatus.NEW,
    )

    # --- attachments ---
    for att in incoming.attachments:
        filename = sanitize_filename(att.filename)
        attachment_signals(sig, filename, att.mime_type, att.data)
        digest = sha256_hex(att.data)
        row = Attachment(
            filename=filename,
            mime_type=att.mime_type[:127],
            size_bytes=len(att.data),
            sha256=digest,
            text_source=TextSource.NONE,
        )
        if len(att.data) > settings.max_attachment_bytes:
            row.parse_error = "exceeds MAX_ATTACHMENT_BYTES; not stored"
            sig.add(
                f"attachment_too_large:{filename}", f"Attachment too large to process: {filename}"
            )
        else:
            row.storage_path = str(store_attachment(settings.attachments_dir, att.data, digest))
        email.attachments.append(row)

    email.spam_signals = sig.signals
    email.review_reasons = sig.review_reasons
    email.needs_manual_review = bool(sig.review_reasons)

    db.add(email)
    try:
        db.flush()
    except IntegrityError:
        # Lost a race with a concurrent ingest of the same message_id.
        db.rollback()
        existing = _find(db, incoming.message_id)
        if existing is None:
            raise
        return _duplicate(db, existing, incoming)

    audit.record(
        db,
        EventType.EMAIL_RECEIVED,
        email=email,
        to_status=EmailStatus.NEW.value,
        details={
            "message_id": incoming.message_id,
            "thread_id": incoming.thread_id,
            "source": incoming.source.value,
            "sender_domain": sender.domain,
            "subject_chars": len(subject),
            "message_chars": len(body),
            "attachments": [a.filename for a in email.attachments],
            "spam_signals": sig.signals,
            "review_reasons": sig.review_reasons,
        },
    )
    db.commit()
    return IngestResult(
        email_id=email.id,
        created=True,
        duplicate=False,
        spam_signals=sig.signals,
        review_reasons=sig.review_reasons,
        needs_manual_review=email.needs_manual_review,
    )
