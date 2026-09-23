"""Pipeline step 0: read attachment text (before classification). Never fails the email:
an unreadable attachment is recorded on the attachment row and flagged for review, and
extraction then lists it as missing information instead of guessing."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from app.attachments.extract import extract_text, tesseract_path
from app.core.categories import get_categories
from app.core.config import get_settings
from app.core.logging import EventType
from app.db.base import TextSource
from app.db.models import Email
from app.ingestion.spam_signals import Signals, content_signals
from app.services import audit


def _flag(email: Email, reason: str) -> None:
    if reason not in email.review_reasons:
        email.review_reasons = [*email.review_reasons, reason]
    email.needs_manual_review = True


def prepare_attachments(db: Session, email: Email) -> None:
    settings = get_settings()
    cmd = tesseract_path(settings.tesseract_cmd)
    root = settings.attachments_dir.resolve()
    results = []
    for att in email.attachments:
        if att.text_source != TextSource.NONE or att.parse_error or not att.storage_path:
            continue  # already done, or recorded as too large / unreadable
        path = Path(att.storage_path).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            att.parse_error = "stored file missing"
            _flag(email, f"Attachment file missing: {att.filename}")
            continue
        r = extract_text(
            path.read_bytes(),
            max_chars=settings.max_attachment_chars,
            ocr_cmd=cmd,
            ocr_enabled=settings.ocr_enabled,
        )
        att.extracted_text = r.text
        att.text_source = r.source
        att.page_count = r.page_count
        att.truncated = r.truncated
        att.parse_error = r.error
        if r.error and not r.text:
            _flag(email, f"Attachment not readable: {att.filename} ({r.error})")
        if r.text:
            # Attachment text is untrusted too: check it for injection/phishing phrases.
            sig = Signals()
            content_signals(
                sig,
                get_categories().spam_signals,
                subject="",
                body=r.text,
                hidden_text="",
                invisible_chars=0,
            )
            for s in sig.signals:
                # keep the code first so classify/draft treat it like a body signal
                code, _, rest = s.partition(":")
                tagged = f"{code}:in_attachment:{rest}" if rest else f"{code}:in_attachment"
                if tagged not in email.spam_signals:
                    email.spam_signals = [*email.spam_signals, tagged]
            for reason in sig.review_reasons:
                _flag(email, f"{reason} (in attachment {att.filename})")
        results.append(
            {
                "filename": att.filename,
                "source": r.source.value,
                "pages": r.page_count,
                "chars": len(r.text or ""),
                "error": r.error,
            }
        )
    if results:
        audit.record(
            db,
            EventType.ATTACHMENTS_PROCESSED,
            email=email,
            details={"attachments": results, "ocr_available": bool(cmd)},
        )
        db.commit()
