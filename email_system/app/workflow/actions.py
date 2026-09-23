"""Admin actions: edit, approve, reject, request edit, regenerate, reopen, retry, send.

Every action needs a named admin, goes through the single transition table and writes
audit entries. `send_email` is the ONLY code path that can deliver a reply, and it
refuses unless every check passes (fail closed):
  1. SEND_MODE allows sending
  2. status is APPROVED (or FAILED after a confirmed, retryable rejection)
  3. no earlier send attempt with an unknown outcome
  4. latest approval is APPROVED, for the current draft, and the SHA-256 of the current
     subject+body equals the hash recorded at approval (any later change blocks sending)
  5. the reply address is a valid email address
"""

from __future__ import annotations

import difflib
import hashlib
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ERROR_POLICIES, ErrorCode, PipelineError
from app.core.logging import EventType
from app.db.base import ApprovalDecision, DraftSource, EmailStatus
from app.db.models import Approval, Draft, Email
from app.ingestion.spam_signals import parse_sender
from app.llm.base import LLMProvider
from app.pipeline.draft import generate_draft
from app.pipeline.draft_checks import check_draft, normalise_subject
from app.pipeline.extract import grounding_source
from app.pipeline.failures import record_failure
from app.sending.base import EmailSender, OutgoingEmail, SendRejected
from app.services import audit
from app.workflow.states import transition

S = EmailStatus
MAX_ADMIN_NAME = 100


def content_hash(subject: str, body: str) -> str:
    return hashlib.sha256(f"{subject}\n\x00\n{body}".encode()).hexdigest()


def _admin(name: str | None) -> str:
    name = " ".join((name or "").split())
    if not name or name.lower() == audit.SYSTEM_ACTOR or len(name) > MAX_ADMIN_NAME:
        raise PipelineError(ErrorCode.INVALID_ADMIN, "admin name required (not 'system')")
    return name


def _require(email: Email, allowed: set[S], action: str) -> S:
    status = S(email.status)
    if status not in allowed:
        raise PipelineError(ErrorCode.INVALID_TRANSITION, f"cannot {action} in {status.value}")
    return status


def _warnings_for(email: Email, subject: str, body: str) -> list[str]:
    s = get_settings()
    src = grounding_source(email, s.max_email_chars, s.max_attachment_chars)
    extracted = str(email.extractions[-1].data) if email.extractions else ""
    return check_draft(subject, body, f"{src.text}\n{extracted}", s.reply_signature)


# ------------------------------------------------------------------------------ edit


def save_draft(
    db: Session,
    email: Email,
    admin: str,
    *,
    subject: str,
    body: str,
    expected_draft_id: int | None = None,
) -> Draft:
    """Admin edit (or first manual draft). Creates a new version; the old one is kept.
    Editing an APPROVED/FAILED email sends it back to UNDER_REVIEW (approval covers the
    exact text only)."""
    admin = _admin(admin)
    status = _require(email, {S.UNDER_REVIEW, S.EDIT_REQUIRED, S.APPROVED, S.FAILED}, "edit")
    if email.send_started_at is not None:
        raise PipelineError(ErrorCode.SEND_DELIVERY_UNKNOWN, "a send attempt is unresolved")
    previous = email.current_draft
    if expected_draft_id is not None and (previous is None or previous.id != expected_draft_id):
        raise PipelineError(ErrorCode.STALE_DRAFT, "draft changed since it was opened")
    subject = normalise_subject(subject, email.subject)
    body = body.strip()
    if not body:
        raise PipelineError(ErrorCode.INVALID_TRANSITION, "draft body cannot be empty")
    if previous is not None and previous.subject == subject and previous.body == body:
        return previous  # nothing changed: no new version, approval (if any) still valid

    old_text = f"Subject: {previous.subject}\n\n{previous.body}" if previous else ""
    new_text = f"Subject: {subject}\n\n{body}"
    diff = "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"v{previous.version}" if previous else "empty",
            tofile="edited",
            n=1,
        )
    )
    if previous is not None:
        previous.is_current = False
        db.flush()
    draft = Draft(
        email=email,
        version=max((d.version for d in email.drafts), default=0) + 1,
        parent_draft_id=previous.id if previous else None,
        source=DraftSource.ADMIN_EDIT,
        subject=subject,
        body=body,
        missing_information=list(previous.missing_information) if previous else [],
        reason_for_reply=previous.reason_for_reply if previous else "Written by admin",
        requires_human_review=True,
        is_current=True,
        model_name=None,
        warnings=_warnings_for(email, subject, body),
        created_by=admin,
    )
    db.add(draft)
    db.flush()
    audit.record(
        db,
        EventType.DRAFT_EDITED,
        email=email,
        actor=admin,
        details={
            "draft_id": draft.id,
            "version": draft.version,
            "from_version": previous.version if previous else None,
            "warnings": draft.warnings,
        },
        db_only={"diff": diff},  # the edit diff is kept in the audit row, never logged
    )
    if status in (S.APPROVED, S.FAILED, S.EDIT_REQUIRED):
        transition(
            db,
            email,
            S.UNDER_REVIEW,
            actor=admin,
            details={"reason": "draft edited; re-approval required"},
        )
    db.commit()
    return draft


# ------------------------------------------------------------------------------ decisions


def approve(
    db: Session,
    email: Email,
    admin: str,
    *,
    draft_id: int,
    acknowledge_warnings: bool = False,
    comment: str | None = None,
) -> Approval:
    admin = _admin(admin)
    _require(email, {S.UNDER_REVIEW}, "approve")
    draft = email.current_draft
    if draft is None:
        raise PipelineError(ErrorCode.INVALID_TRANSITION, "no draft to approve")
    if draft.id != draft_id:
        raise PipelineError(
            ErrorCode.STALE_DRAFT, f"current draft is v{draft.version}, not the one reviewed"
        )
    if draft.warnings and not acknowledge_warnings:
        raise PipelineError(
            ErrorCode.WARNINGS_NOT_ACKNOWLEDGED, f"{len(draft.warnings)} warning(s)"
        )
    digest = content_hash(draft.subject, draft.body)
    row = Approval(
        email=email,
        draft_id=draft.id,
        decision=ApprovalDecision.APPROVED,
        approver=admin,
        comment=(comment or None),
        content_sha256=digest,
    )
    db.add(row)
    transition(db, email, S.APPROVED, actor=admin)
    audit.record(
        db,
        EventType.ADMIN_APPROVED,
        email=email,
        actor=admin,
        details={
            "draft_id": draft.id,
            "version": draft.version,
            "content_sha256": digest,
            "warnings_acknowledged": bool(draft.warnings),
        },
    )
    db.commit()
    return row


def reject(db: Session, email: Email, admin: str, *, reason: str | None = None) -> Approval:
    admin = _admin(admin)
    _require(email, {S.UNDER_REVIEW, S.EDIT_REQUIRED, S.APPROVED, S.FAILED}, "reject")
    if email.send_started_at is not None:
        raise PipelineError(ErrorCode.SEND_DELIVERY_UNKNOWN, "a send attempt is unresolved")
    draft = email.current_draft
    row = Approval(
        email=email,
        draft_id=draft.id if draft else None,
        decision=ApprovalDecision.REJECTED,
        approver=admin,
        comment=reason or None,
    )
    db.add(row)
    transition(db, email, S.REJECTED, actor=admin)
    audit.record(
        db,
        EventType.ADMIN_REJECTED,
        email=email,
        actor=admin,
        details={"draft_id": draft.id if draft else None, "has_reason": bool(reason)},
    )
    db.commit()
    return row


def request_edit(db: Session, email: Email, admin: str, *, comment: str) -> Approval:
    admin = _admin(admin)
    _require(email, {S.UNDER_REVIEW}, "request an edit")
    draft = email.current_draft
    row = Approval(
        email=email,
        draft_id=draft.id if draft else None,
        decision=ApprovalDecision.EDIT_REQUIRED,
        approver=admin,
        comment=comment,
    )
    db.add(row)
    transition(db, email, S.EDIT_REQUIRED, actor=admin)
    audit.record(
        db,
        EventType.EDIT_REQUESTED,
        email=email,
        actor=admin,
        details={"draft_id": draft.id if draft else None},
    )
    db.commit()
    return row


def reopen(db: Session, email: Email, admin: str) -> None:
    admin = _admin(admin)
    _require(email, {S.REJECTED}, "reopen")
    transition(db, email, S.UNDER_REVIEW, actor=admin)
    audit.record(db, EventType.REOPENED, email=email, actor=admin)
    db.commit()


def regenerate(
    db: Session,
    email: Email,
    admin: str,
    provider: LLMProvider,
    *,
    instructions: str | None = None,
) -> Draft | None:
    """Admin asks for a new AI draft (explicit request => allowed even for spam/red-flag
    emails). An approved draft loses its approval first."""
    admin = _admin(admin)
    status = _require(email, {S.UNDER_REVIEW, S.EDIT_REQUIRED, S.APPROVED}, "regenerate")
    if status == S.APPROVED:
        transition(
            db,
            email,
            S.UNDER_REVIEW,
            actor=admin,
            details={"reason": "regenerate requested; approval withdrawn"},
        )
        db.commit()
    return generate_draft(
        db, email, provider, actor=admin, admin_instructions=instructions, force=True
    )


def retry_processing(db: Session, email: Email, admin: str) -> None:
    """ERROR -> NEW so the pipeline can run again (the caller runs process_email)."""
    admin = _admin(admin)
    _require(email, {S.ERROR}, "retry")
    code = email.last_error_code
    if code and code in ErrorCode.__members__ and not ERROR_POLICIES[ErrorCode(code)].retryable:
        raise PipelineError(ErrorCode.NOT_RETRYABLE, f"last error {code} is not retryable")
    transition(db, email, S.NEW, actor=admin)
    audit.record(
        db, EventType.RETRY_REQUESTED, email=email, actor=admin, details={"last_error_code": code}
    )
    db.commit()


# ------------------------------------------------------------------------------ send


def _check_sendable(email: Email) -> tuple[Draft, Approval, str]:
    status = S(email.status)
    if status == S.SENT:
        raise PipelineError(ErrorCode.SEND_NOT_APPROVED, "already sent")
    if email.send_started_at is not None:
        raise PipelineError(
            ErrorCode.SEND_DELIVERY_UNKNOWN, "an earlier send attempt has an unknown outcome"
        )
    if status == S.FAILED:
        code = email.last_error_code
        if code != ErrorCode.SEND_FAILED.value:
            raise PipelineError(ErrorCode.NOT_RETRYABLE, f"last send error {code}")
    elif status != S.APPROVED:
        raise PipelineError(ErrorCode.SEND_NOT_APPROVED, f"status is {status.value}")
    draft = email.current_draft
    approval = email.approvals[-1] if email.approvals else None
    if draft is None or approval is None or approval.decision != ApprovalDecision.APPROVED:
        raise PipelineError(ErrorCode.SEND_NOT_APPROVED, "no approval for the current draft")
    if approval.draft_id != draft.id:
        raise PipelineError(ErrorCode.SEND_NOT_APPROVED, "approval is for another draft")
    if approval.content_sha256 != content_hash(draft.subject, draft.body):
        raise PipelineError(ErrorCode.SEND_NOT_APPROVED, "draft changed after approval")
    to = parse_sender(email.sender)
    if not to.valid:
        raise PipelineError(ErrorCode.INVALID_RECIPIENT, "reply address invalid")
    return draft, approval, to.address


def send_email(db: Session, email: Email, admin: str, sender: EmailSender) -> str:
    """Deliver the approved reply. Returns the provider message id."""
    admin = _admin(admin)
    draft, approval, to = _check_sendable(email)
    from_status = S(email.status)

    # Durable marker BEFORE contacting the provider: if we crash after this point the
    # outcome is unknown and resending is blocked until a human checks.
    email.send_started_at = datetime.now(UTC)
    audit.record(
        db,
        EventType.SEND_STARTED,
        email=email,
        actor=admin,
        details={"draft_id": draft.id, "approval_id": approval.id, "sender": sender.name},
    )
    db.commit()

    message = OutgoingEmail(
        to=to,
        subject=draft.subject,
        body=draft.body,
        in_reply_to=email.message_id,
        thread_id=email.thread_id,
    )
    try:
        provider_id = sender.send(message)
    except SendRejected as exc:
        email.send_started_at = None  # confirmed not delivered: a retry is safe
        err = PipelineError(ErrorCode.SEND_FAILED, f"{sender.name}: {type(exc).__name__}")
        record_failure(db, email, "send", err)
        raise err from None
    except Exception as exc:  # noqa: BLE001  timeout, crash, unknown: may have been sent
        err = PipelineError(ErrorCode.SEND_DELIVERY_UNKNOWN, f"{sender.name}: {type(exc).__name__}")
        record_failure(db, email, "send", err)  # send_started_at stays set => blocked
        raise err from None

    email.sent_at = datetime.now(UTC)
    email.sent_provider_message_id = provider_id[:255]
    email.send_started_at = None
    transition(db, email, S.SENT, actor=admin, details={"from": from_status.value})
    audit.record(
        db,
        EventType.EMAIL_SENT,
        email=email,
        actor=admin,
        details={
            "draft_id": draft.id,
            "version": draft.version,
            "approval_id": approval.id,
            "approved_by": approval.approver,
            "content_sha256": approval.content_sha256,
            "provider": sender.name,
            "provider_message_id": provider_id,
        },
    )
    db.commit()
    return provider_id
