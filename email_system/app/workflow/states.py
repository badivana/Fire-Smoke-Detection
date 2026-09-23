"""The one place where email status changes are allowed and recorded.

Every status change in the app goes through `transition()`. It checks the table below,
updates the row and writes a STATE_CHANGED audit entry. Key invariants (tested):
  - APPROVED is reachable only from UNDER_REVIEW (a human decision).
  - SENT is reachable only from APPROVED (or FAILED, i.e. retrying an approved send).
  - SENT is terminal.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import ErrorCode, PipelineError
from app.core.logging import EventType
from app.db.base import EmailStatus as S
from app.db.models import Email
from app.services import audit

ALLOWED: dict[S, frozenset[S]] = {
    S.NEW: frozenset({S.CLASSIFIED, S.ERROR}),
    S.CLASSIFIED: frozenset({S.DRAFT_GENERATED, S.UNDER_REVIEW, S.ERROR}),
    S.DRAFT_GENERATED: frozenset({S.UNDER_REVIEW, S.ERROR}),
    S.UNDER_REVIEW: frozenset(
        {S.APPROVED, S.REJECTED, S.EDIT_REQUIRED, S.DRAFT_GENERATED, S.ERROR}
    ),
    S.EDIT_REQUIRED: frozenset({S.UNDER_REVIEW, S.DRAFT_GENERATED, S.REJECTED, S.ERROR}),
    # Editing an approved draft sends it back for review; approval covers exact text only.
    S.APPROVED: frozenset({S.SENT, S.FAILED, S.UNDER_REVIEW, S.REJECTED}),
    S.FAILED: frozenset({S.SENT, S.UNDER_REVIEW, S.REJECTED}),
    S.REJECTED: frozenset({S.UNDER_REVIEW}),
    # Retry: reset to NEW and run the pipeline again.
    S.ERROR: frozenset({S.NEW}),
    S.SENT: frozenset(),
}


def can_transition(current: S, target: S) -> bool:
    return target in ALLOWED[current]


def transition(
    db: Session,
    email: Email,
    target: S,
    *,
    actor: str = audit.SYSTEM_ACTOR,
    details: dict[str, Any] | None = None,
) -> None:
    current = S(email.status)
    if not can_transition(current, target):
        raise PipelineError(
            ErrorCode.INVALID_TRANSITION, f"{current.value} -> {target.value} not allowed"
        )
    email.status = target
    audit.record(
        db,
        EventType.STATE_CHANGED,
        email=email,
        actor=actor,
        from_status=current.value,
        to_status=target.value,
        details=details,
    )
