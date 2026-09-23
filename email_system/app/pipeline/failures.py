"""Record a pipeline failure: error row, audit entry, move to ERROR/FAILED. Never sends."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.errors import FailureState, PipelineError
from app.core.logging import EventType
from app.db.base import EmailStatus
from app.db.models import Email, ProcessingError
from app.services import audit
from app.workflow.states import can_transition, transition


def record_failure(db: Session, email: Email, stage: str, err: PipelineError) -> None:
    policy = err.policy
    db.add(
        ProcessingError(
            email=email,
            stage=stage,
            error_code=err.code.value,
            detail=err.detail,
            retryable=policy.retryable,
        )
    )
    email.last_error_code = err.code.value
    audit.record(
        db,
        EventType.ERROR,
        email=email,
        details={"stage": stage, "error_code": err.code.value, "detail": err.detail},
    )
    target = {FailureState.ERROR: EmailStatus.ERROR, FailureState.FAILED: EmailStatus.FAILED}
    to = target.get(policy.state)
    if to is not None and can_transition(EmailStatus(email.status), to):
        transition(db, email, to, details={"error_code": err.code.value})
    db.commit()
