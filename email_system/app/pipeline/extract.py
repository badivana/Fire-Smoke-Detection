"""Extraction step for CLASSIFIED emails whose category has an extraction schema.

Status stays CLASSIFIED (the spec has no EXTRACTED state); an EXTRACTED audit event is
written. Failures move the email to ERROR. Never drafts, approves or sends.

Rule 2 (no invented facts) is enforced twice: by the prompt, and by the deterministic
grounding check, which removes any value not found in the email and flags the email.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.categories import ExtractionSchema, get_categories
from app.core.config import get_settings
from app.core.errors import ErrorCode, PipelineError
from app.core.logging import EventType
from app.db.base import EmailStatus
from app.db.models import Email, Extraction
from app.llm.base import LLMProvider
from app.llm.structured import call_structured
from app.pipeline.failures import record_failure
from app.pipeline.grounding import Source, ground
from app.pipeline.prompts import EXTRACT_PROMPT_VERSION, build_extraction_prompt
from app.pipeline.schemas import EXTRACTION_MODELS, inline_json_schema
from app.services import audit


def _flag(email: Email, reason: str) -> None:
    if reason not in email.review_reasons:
        email.review_reasons = [*email.review_reasons, reason]
    email.needs_manual_review = True


def _clean_notes(notes: list[str]) -> list[str]:
    out: list[str] = []
    for n in notes:
        n = " ".join(n.split())[:120]
        if n and n.lower() not in {o.lower() for o in out}:
            out.append(n)
    return out


def grounding_source(email: Email, max_chars: int, attachment_chars: int) -> Source:
    """Only sender-written content, as the model saw it (same truncation limits).

    Prompt metadata (delimiter nonce, timestamps, our instructions) is excluded: it
    contains digits and words that would otherwise 'confirm' invented values.
    """
    parts = [
        email.sender_name or "",
        email.sender,
        email.subject,
        (email.body_text or "")[:max_chars],
    ]
    parts += [(a.extracted_text or "")[:attachment_chars] for a in email.attachments]
    return Source(
        text="\n".join(parts), attachment_names=tuple(a.filename for a in email.attachments)
    )


def extract_email(db: Session, email: Email, provider: LLMProvider) -> Extraction | None:
    if email.status != EmailStatus.CLASSIFIED:
        raise PipelineError(
            ErrorCode.INVALID_TRANSITION, f"cannot extract from an email in {email.status}"
        )
    category, _ = get_categories().resolve(email.category)
    schema_name = category.extraction_schema.value
    if category.extraction_schema == ExtractionSchema.NONE:
        return None

    settings = get_settings()
    model_cls = EXTRACTION_MODELS[schema_name]
    prompt = build_extraction_prompt(
        email,
        schema_name,
        max_chars=settings.max_email_chars,
        attachment_chars=settings.max_attachment_chars,
    )
    try:
        result = call_structured(
            provider,
            system=prompt.system,
            user=prompt.user,
            output_model=model_cls,
            schema=inline_json_schema(model_cls),
            schema_name=f"{schema_name}_extraction",
            max_retries=settings.llm_max_retries,
        )
    except PipelineError as err:
        record_failure(db, email, "extract", err)
        raise

    source = grounding_source(email, settings.max_email_chars, settings.max_attachment_chars)
    raw = result.value.model_dump(mode="json")
    llm_notes = raw.pop("missing_information")
    g = ground(schema_name, raw, source)

    missing = list(g.missing)
    unread = [a.filename for a in email.attachments if not a.extracted_text]
    for name in unread:
        missing.append(f"attachment content not read: {name}")
    missing += [n for n in _clean_notes(llm_notes) if n.lower() not in {m.lower() for m in missing}]

    row = Extraction(
        email=email,
        schema_name=schema_name,
        data=g.data,
        missing_information=missing,
        ungrounded_fields=g.ungrounded,
        model_name=f"{provider.name}:{result.model}",
        prompt_version=EXTRACT_PROMPT_VERSION,
        llm_attempts=result.attempts,
    )
    db.add(row)
    if g.ungrounded:
        _flag(
            email,
            "Extracted values not found in the email were removed: " + ", ".join(g.ungrounded),
        )
    if unread:
        _flag(email, "Attachment not read yet: " + ", ".join(unread))
    if prompt.truncated:
        _flag(email, "Email or attachment was truncated before extraction")

    audit.record(
        db,
        EventType.EXTRACTED,
        email=email,
        model_name=row.model_name,
        details={
            "schema": schema_name,
            "fields_found": sorted(k for k, v in g.data.items() if v not in (None, [], "")),
            "missing_count": len(missing),
            "ungrounded_fields": g.ungrounded,
            "items": len(g.data.get("items") or []),
            "llm_attempts": result.attempts,
            "duration_ms": result.duration_ms,
            "prompt_version": EXTRACT_PROMPT_VERSION,
        },
    )
    db.commit()
    return row
