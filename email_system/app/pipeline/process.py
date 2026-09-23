"""Run the whole AI pipeline for one email:
read attachments -> classify -> extract -> draft -> UNDER_REVIEW.

Stops at the first failure (which has already moved the email to ERROR and been
recorded). The result is always an email waiting for a human, or in ERROR. Never sends.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.base import EmailStatus
from app.db.models import Email
from app.llm.base import LLMProvider
from app.pipeline.attachments import prepare_attachments
from app.pipeline.classify import classify_email
from app.pipeline.draft import generate_draft
from app.pipeline.extract import extract_email


def process_email(db: Session, email: Email, provider: LLMProvider) -> EmailStatus:
    if email.status == EmailStatus.NEW:
        prepare_attachments(db, email)
        classify_email(db, email, provider)
    if email.status == EmailStatus.CLASSIFIED:
        extract_email(db, email, provider)
        generate_draft(db, email, provider)
    return EmailStatus(email.status)
