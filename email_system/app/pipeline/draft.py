"""Drafting step: CLASSIFIED -> DRAFT_GENERATED -> UNDER_REVIEW, or CLASSIFIED ->
UNDER_REVIEW with no draft when
  - the category has draft_reply: false (e.g. IRRELEVANT), or
  - the email has red flags (LLM red flags or rule-based spam/injection signals).
    Measured: a misclassified injection email got a friendly draft repeating the
    attacker's "pre-approved" claim. Suspicious emails get a draft only on explicit
    admin request (force=True).

Also used for "regenerate" from UNDER_REVIEW / EDIT_REQUIRED. Every draft has
requires_human_review=True (schema Literal + DB CHECK). Never approves or sends.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.categories import get_categories
from app.core.config import get_settings
from app.core.errors import ErrorCode, PipelineError
from app.core.logging import EventType
from app.db.base import DraftSource, EmailStatus
from app.db.models import Draft, Email
from app.llm.base import LLMProvider
from app.llm.structured import call_structured
from app.pipeline.classify import SPAMMY_SIGNALS
from app.pipeline.draft_checks import check_draft, ensure_signature, normalise_subject
from app.pipeline.extract import grounding_source
from app.pipeline.failures import record_failure
from app.pipeline.prompts import DRAFT_PROMPT_VERSION, build_draft_prompt
from app.pipeline.schemas import DraftOutput, inline_json_schema
from app.services import audit
from app.workflow.states import transition

DRAFTABLE_FROM = {EmailStatus.CLASSIFIED, EmailStatus.UNDER_REVIEW, EmailStatus.EDIT_REQUIRED}


def _flag(email: Email, reason: str) -> None:
    if reason not in email.review_reasons:
        email.review_reasons = [*email.review_reasons, reason]
    email.needs_manual_review = True


def move_to_review_without_draft(db: Session, email: Email, reason: str) -> None:
    _flag(email, reason)
    audit.record(
        db,
        EventType.NO_DRAFT_NEEDED,
        email=email,
        details={"category": email.category, "reason": reason},
    )
    transition(db, email, EmailStatus.UNDER_REVIEW)
    audit.record(db, EventType.MOVED_TO_REVIEW, email=email, details={"draft": False})
    db.commit()


def red_flags(email: Email) -> list[str]:
    flags: set[str] = set()
    if email.classifications:
        flags.update(email.classifications[-1].red_flags)
    flags.update(
        s.split(":", 1)[0] for s in email.spam_signals if s.split(":", 1)[0] in SPAMMY_SIGNALS
    )
    return sorted(flags)


def generate_draft(
    db: Session,
    email: Email,
    provider: LLMProvider,
    *,
    actor: str = audit.SYSTEM_ACTOR,
    admin_instructions: str | None = None,
    force: bool = False,
) -> Draft | None:
    """Create a new current draft. `force` = admin explicitly asked for a draft for a
    category that normally gets none (e.g. IRRELEVANT)."""
    status = EmailStatus(email.status)
    if status not in DRAFTABLE_FROM:
        raise PipelineError(ErrorCode.INVALID_TRANSITION, f"cannot draft in {status.value}")

    category, _ = get_categories().resolve(email.category)
    if not force:
        reason = None
        if not category.draft_reply:
            reason = f"No reply recommended ({category.name}); confirm or reject"
        elif flags := red_flags(email):
            reason = (
                f"No draft generated because of red flags ({', '.join(flags)}); "
                "request one explicitly if the email is genuine"
            )
        if reason:
            if status == EmailStatus.CLASSIFIED:
                move_to_review_without_draft(db, email, reason)
            return None

    settings = get_settings()
    extraction = email.extractions[-1] if email.extractions else None
    missing = list(extraction.missing_information) if extraction else []
    prompt = build_draft_prompt(
        email,
        category=category.name,
        extracted=extraction.data if extraction else None,
        missing=missing,
        institution=settings.institution_name,
        signature=settings.reply_signature,
        max_chars=settings.max_email_chars,
        attachment_chars=settings.max_attachment_chars,
        admin_instructions=admin_instructions,
    )
    try:
        result = call_structured(
            provider,
            system=prompt.system,
            user=prompt.user,
            output_model=DraftOutput,
            schema=inline_json_schema(DraftOutput),
            schema_name="reply_draft",
            max_retries=settings.llm_max_retries,
        )
    except PipelineError as err:
        record_failure(db, email, "draft", err)
        raise

    out = result.value
    subject = normalise_subject(out.subject, email.subject)
    body = ensure_signature(out.body, settings.reply_signature)
    src = grounding_source(email, settings.max_email_chars, settings.max_attachment_chars)
    extracted_text = str(extraction.data) if extraction else ""
    warnings = check_draft(subject, body, f"{src.text}\n{extracted_text}", settings.reply_signature)

    draft_missing = list(missing)
    for m in out.missing_information:
        m = " ".join(m.split())[:120]
        if m and m.lower() not in {x.lower() for x in draft_missing}:
            draft_missing.append(m)

    previous = email.current_draft
    if previous is not None:
        previous.is_current = False
        db.flush()  # partial unique index: only one current draft per email
    draft = Draft(
        email=email,
        version=(max((d.version for d in email.drafts), default=0) + 1),
        parent_draft_id=previous.id if previous else None,
        source=DraftSource.AI,
        subject=subject,
        body=body,
        missing_information=draft_missing,
        reason_for_reply=out.reason_for_reply.strip(),
        requires_human_review=True,
        is_current=True,
        model_name=f"{provider.name}:{result.model}",
        prompt_version=DRAFT_PROMPT_VERSION,
        warnings=warnings,
        created_by=actor,
    )
    db.add(draft)
    db.flush()
    if warnings:
        _flag(email, f"Draft v{draft.version} has {len(warnings)} automatic warning(s)")

    audit.record(
        db,
        EventType.DRAFT_GENERATED,
        email=email,
        actor=actor,
        model_name=draft.model_name,
        details={
            "draft_id": draft.id,
            "version": draft.version,
            "regenerated": previous is not None,
            "admin_instructions_given": bool(admin_instructions),
            "forced": force,
            "warnings": warnings,
            "missing_count": len(draft_missing),
            "llm_attempts": result.attempts,
            "duration_ms": result.duration_ms,
            "prompt_version": DRAFT_PROMPT_VERSION,
        },
    )
    transition(db, email, EmailStatus.DRAFT_GENERATED, actor=actor)
    transition(db, email, EmailStatus.UNDER_REVIEW, actor=actor)
    audit.record(
        db,
        EventType.MOVED_TO_REVIEW,
        email=email,
        actor=actor,
        details={"draft_id": draft.id, "version": draft.version},
    )
    db.commit()
    return draft
