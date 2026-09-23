"""Email queue, detail, pipeline and approval endpoints.

Every route requires X-Admin-Key when ADMIN_API_KEY is set. Every state-changing route
also requires X-Admin-Name (recorded as the actor in the audit trail). Sending is only
possible through POST /emails/{id}/send, which uses the guarded workflow.send_email.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import (
    admin_name,
    get_db,
    get_llm_provider,
    get_sender_factory,
    require_admin,
)
from app.api.schemas import (
    ApproveRequest,
    AuditOut,
    DraftEdit,
    EditRequest,
    EmailDetail,
    EmailList,
    EmailSummary,
    RegenerateRequest,
    RejectRequest,
    SendResult,
)
from app.core.errors import ERROR_POLICIES, ErrorCode, PipelineError
from app.db.base import EmailStatus
from app.db.models import AuditLog, Draft, Email
from app.llm.base import LLMProvider
from app.pipeline.process import process_email
from app.sending.base import EmailSender
from app.workflow import actions

router = APIRouter(prefix="/emails", tags=["emails"], dependencies=[Depends(require_admin)])

DB = Annotated[Session, Depends(get_db)]
Admin = Annotated[str, Depends(admin_name)]
LLM = Annotated[LLMProvider, Depends(get_llm_provider)]
Sender = Annotated[Callable[[], EmailSender], Depends(get_sender_factory)]
S = EmailStatus


def _get(db: Session, email_id: int) -> Email:
    email = db.get(Email, email_id)
    if email is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "email not found")
    return email


def allowed_actions(email: Email) -> list[str]:
    """What the dashboard may offer. The workflow re-checks everything server-side."""
    st = S(email.status)
    if email.send_started_at is not None:
        return []  # unresolved send attempt: view only
    has_draft = email.current_draft is not None
    acts: list[str] = []
    if st in (S.NEW, S.CLASSIFIED):
        acts.append("process")
    if st in (S.UNDER_REVIEW, S.EDIT_REQUIRED, S.APPROVED, S.FAILED):
        acts.append("edit")
    if st in (S.UNDER_REVIEW, S.EDIT_REQUIRED, S.APPROVED):
        acts.append("regenerate")
    if st == S.UNDER_REVIEW:
        acts += (["approve"] if has_draft else []) + ["request_edit"]
    if st in (S.UNDER_REVIEW, S.EDIT_REQUIRED, S.APPROVED, S.FAILED):
        acts.append("reject")
    if st == S.APPROVED:
        acts.append("send")
    if st == S.REJECTED:
        acts.append("reopen")
    if st == S.ERROR:
        code = email.last_error_code
        if not (code in ErrorCode.__members__ and not ERROR_POLICIES[ErrorCode(code)].retryable):
            acts.append("retry")
    if st == S.FAILED and email.last_error_code == ErrorCode.SEND_FAILED.value:
        acts.append("retry")
    return acts


def detail(email: Email) -> EmailDetail:
    return EmailDetail.model_validate(
        {
            **{
                c: getattr(email, c)
                for c in (
                    "id",
                    "sender",
                    "sender_name",
                    "subject",
                    "category",
                    "priority",
                    "status",
                    "received_at",
                    "needs_manual_review",
                    "message_id",
                    "thread_id",
                    "recipients",
                    "body_text",
                    "review_reasons",
                    "spam_signals",
                    "processing_attempts",
                    "last_error_code",
                    "send_started_at",
                    "sent_at",
                    "sent_provider_message_id",
                    "attachments",
                    "approvals",
                )
            },
            "has_draft": email.current_draft is not None,
            "classification": email.classifications[-1] if email.classifications else None,
            "extraction": email.extractions[-1] if email.extractions else None,
            "draft": email.current_draft,
            "draft_versions": email.drafts,
            "allowed_actions": allowed_actions(email),
        }
    )


# ------------------------------------------------------------------------ read


@router.get("", response_model=EmailList)
def list_emails(
    db: DB,
    status_: Annotated[S | None, Query(alias="status")] = None,
    category: str | None = None,
    needs_review: bool | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EmailList:
    stmt = select(Email)
    if status_ is not None:
        stmt = stmt.where(Email.status == status_)
    if category:
        stmt = stmt.where(Email.category == category.upper())
    if needs_review is not None:
        stmt = stmt.where(Email.needs_manual_review == needs_review)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            func.lower(Email.subject).like(like) | func.lower(Email.sender).like(like)
        )
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(Email.received_at.desc(), Email.id.desc()).limit(limit).offset(offset)
    ).all()
    current = set(
        db.scalars(
            select(Draft.email_id).where(
                Draft.is_current.is_(True), Draft.email_id.in_([r.id for r in rows])
            )
        ).all()
    )
    items = [
        EmailSummary.model_validate(r).model_copy(update={"has_draft": r.id in current})
        for r in rows
    ]
    return EmailList(total=total, items=items)


@router.get("/{email_id}", response_model=EmailDetail)
def get_email(email_id: int, db: DB) -> EmailDetail:
    email = db.scalars(
        select(Email)
        .where(Email.id == email_id)
        .options(
            selectinload(Email.attachments),
            selectinload(Email.drafts),
            selectinload(Email.classifications),
            selectinload(Email.extractions),
            selectinload(Email.approvals),
        )
    ).first()
    if email is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "email not found")
    return detail(email)


@router.get("/{email_id}/audit", response_model=list[AuditOut])
def get_audit(email_id: int, db: DB) -> list[AuditLog]:
    _get(db, email_id)
    return list(
        db.scalars(
            select(AuditLog).where(AuditLog.email_id == email_id).order_by(AuditLog.id)
        ).all()
    )


# ------------------------------------------------------------------------ AI pipeline


@router.post("/{email_id}/process", response_model=EmailDetail)
def process(email_id: int, db: DB, admin: Admin, llm: LLM) -> EmailDetail:
    """Classify -> extract -> draft -> UNDER_REVIEW. Synchronous (can take minutes on CPU).
    On an LLM failure the email is in ERROR and the error is returned."""
    email = _get(db, email_id)
    if email.status not in (S.NEW, S.CLASSIFIED):
        raise PipelineError(
            ErrorCode.INVALID_TRANSITION,
            f"cannot process an email in {email.status}; use retry/regenerate",
        )
    process_email(db, email, llm)
    return detail(email)


@router.post("/{email_id}/regenerate-draft", response_model=EmailDetail)
def regenerate_draft(
    email_id: int, db: DB, admin: Admin, llm: LLM, req: RegenerateRequest | None = None
) -> EmailDetail:
    email = _get(db, email_id)
    actions.regenerate(db, email, admin, llm, instructions=(req.instructions if req else None))
    return detail(email)


@router.post("/{email_id}/retry", response_model=EmailDetail)
def retry(email_id: int, db: DB, admin: Admin, llm: LLM, sender: Sender) -> EmailDetail:
    """ERROR -> reprocess. FAILED (confirmed rejection) -> resend the approved draft."""
    email = _get(db, email_id)
    if email.status == S.FAILED:
        actions.send_email(db, email, admin, sender())
    else:
        actions.retry_processing(db, email, admin)
        process_email(db, email, llm)
    return detail(email)


# ------------------------------------------------------------------------ admin decisions


@router.put("/{email_id}/draft", response_model=EmailDetail)
def save_draft(email_id: int, req: DraftEdit, db: DB, admin: Admin) -> EmailDetail:
    email = _get(db, email_id)
    actions.save_draft(
        db,
        email,
        admin,
        subject=req.subject,
        body=req.body,
        expected_draft_id=req.expected_draft_id,
    )
    return detail(email)


@router.post("/{email_id}/approve", response_model=EmailDetail)
def approve(email_id: int, req: ApproveRequest, db: DB, admin: Admin) -> EmailDetail:
    email = _get(db, email_id)
    actions.approve(
        db,
        email,
        admin,
        draft_id=req.draft_id,
        acknowledge_warnings=req.acknowledge_warnings,
        comment=req.comment,
    )
    return detail(email)


@router.post("/{email_id}/reject", response_model=EmailDetail)
def reject(email_id: int, db: DB, admin: Admin, req: RejectRequest | None = None) -> EmailDetail:
    email = _get(db, email_id)
    actions.reject(db, email, admin, reason=(req.reason if req else None))
    return detail(email)


@router.post("/{email_id}/request-edit", response_model=EmailDetail)
def request_edit(email_id: int, req: EditRequest, db: DB, admin: Admin) -> EmailDetail:
    email = _get(db, email_id)
    actions.request_edit(db, email, admin, comment=req.comment)
    return detail(email)


@router.post("/{email_id}/reopen", response_model=EmailDetail)
def reopen(email_id: int, db: DB, admin: Admin) -> EmailDetail:
    email = _get(db, email_id)
    actions.reopen(db, email, admin)
    return detail(email)


@router.post("/{email_id}/send", response_model=SendResult)
def send(email_id: int, db: DB, admin: Admin, sender: Sender) -> SendResult:
    """Only sends an APPROVED email whose draft still matches the approved text."""
    email = _get(db, email_id)
    provider_id = actions.send_email(db, email, admin, sender())
    return SendResult(email_id=email.id, status=email.status, provider_message_id=provider_id)
