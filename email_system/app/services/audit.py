"""Audit trail: one DB row + one log line per event. Details are redacted before storage."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import EventType, log_event, redact, scrub_secrets
from app.db.models import AuditLog, Email

SYSTEM_ACTOR = "system"
_DB_ONLY_MAX = 20_000


def scrub_long(value: Any) -> Any:
    if isinstance(value, str):
        return scrub_secrets(value)[:_DB_ONLY_MAX]
    return value


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
    db_only: dict[str, Any] | None = None,
) -> AuditLog:
    """`details` are redacted and also logged. `db_only` (e.g. an admin's draft diff) is
    stored in the audit row only, never written to the log; secrets are still scrubbed."""
    safe = redact(details or {})
    stored = {**safe, **{k: scrub_long(v) for k, v in (db_only or {}).items()}}
    row = AuditLog(
        email=email,
        event_type=event.value,
        actor=actor,
        from_status=from_status,
        to_status=to_status,
        model_name=model_name,
        details=stored,
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
