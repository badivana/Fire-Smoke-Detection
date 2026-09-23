"""Audit trail: one DB row + one log line per event. Details are redacted before storage."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import EventType, log_event, redact
from app.db.models import AuditLog, Email

SYSTEM_ACTOR = "system"


def record(
    db: Session,
    event: EventType,
    *,
    email: Email | None = None,
    actor: str = SYSTEM_ACTOR,
    from_status: str | None = None,
    to_status: str | None = None,
    model_name: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    safe = redact(details or {})
    row = AuditLog(
        email=email,
        event_type=event.value,
        actor=actor,
        from_status=from_status,
        to_status=to_status,
        model_name=model_name,
        details=safe,
    )
    db.add(row)
    log_event(
        event,
        email_id=email.id if email is not None else None,
        actor=actor,
        from_status=from_status,
        to_status=to_status,
        **safe,
    )
    return row
