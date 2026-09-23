"""Classification step: NEW -> CLASSIFIED (or ERROR). Never drafts, approves or sends.

Confidence is not trusted blindly. The email is flagged for manual review when:
  - confidence < CLASSIFICATION_CONFIDENCE_THRESHOLD
  - the LLM reports any red flag (instructions to AI, credential request, ...)
  - the LLM named a category that is not configured (-> fallback category)
  - rule-based spam/injection signals disagree with a non-IRRELEVANT category
Schema failure (after 1 stricter retry), timeout or an unreachable LLM -> ERROR.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.categories import get_categories
from app.core.config import get_settings
from app.core.errors import ErrorCode, PipelineError
from app.core.logging import EventType
from app.db.base import EmailStatus
from app.db.models import Classification, Email
from app.llm.base import LLMProvider
from app.llm.structured import call_structured
from app.pipeline.failures import record_failure
from app.pipeline.prompts import CLASSIFY_PROMPT_VERSION, build_classification_prompt
from app.pipeline.schemas import ClassificationOutput, classification_json_schema
from app.services import audit
from app.workflow.states import can_transition, transition

SPAM_CATEGORY = "IRRELEVANT"
# Rule-based signals that mean "probably spam/phishing/injection".
SPAMMY_SIGNALS = frozenset(
    {
        "gmail_label",
        "suspicious_phrase",
        "hidden_html_text",
        "dangerous_attachment",
        "attachment_type_mismatch",
    }
)


def _add_reason(email: Email, reason: str) -> None:
    if reason not in email.review_reasons:
        email.review_reasons = [*email.review_reasons, reason]
    email.needs_manual_review = True


def classify_email(db: Session, email: Email, provider: LLMProvider) -> Classification:
    settings = get_settings()
    categories = get_categories()

    if not can_transition(EmailStatus(email.status), EmailStatus.CLASSIFIED):
        raise PipelineError(
            ErrorCode.INVALID_TRANSITION, f"cannot classify an email in {email.status}"
        )
    if email.processing_attempts >= settings.max_processing_attempts:
        raise PipelineError(
            ErrorCode.MAX_ATTEMPTS_EXCEEDED, f"{email.processing_attempts} attempts used"
        )
    email.processing_attempts += 1

    prompt = build_classification_prompt(email, categories, max_chars=settings.max_email_chars)
    try:
        result = call_structured(
            provider,
            system=prompt.system,
            user=prompt.user,
            output_model=ClassificationOutput,
            schema=classification_json_schema(categories),
            schema_name="email_classification",
            max_retries=settings.llm_max_retries,
        )
    except PipelineError as err:
        record_failure(db, email, "classify", err)
        raise

    out = result.value
    cat, unknown = categories.resolve(out.category)
    low_conf = out.confidence < settings.classification_confidence_threshold
    signal_codes = {s.split(":", 1)[0] for s in email.spam_signals}
    conflict = cat.name != SPAM_CATEGORY and bool(signal_codes & SPAMMY_SIGNALS)

    row = Classification(
        email=email,
        category=cat.name,
        raw_category=out.category[:100],
        confidence=out.confidence,
        reason=out.reason.strip(),
        priority=out.priority.value,
        requires_action=out.requires_action,
        unknown_category=unknown,
        low_confidence=low_conf,
        model_name=f"{provider.name}:{result.model}",
        prompt_version=CLASSIFY_PROMPT_VERSION,
        llm_attempts=result.attempts,
        red_flags=[f.value for f in out.red_flags],
    )
    db.add(row)
    email.category = cat.name
    email.priority = out.priority.value
    if low_conf:
        _add_reason(email, f"Low classification confidence ({out.confidence:.2f})")
    if unknown:
        _add_reason(email, f"LLM returned unknown category; used {cat.name}")
    if conflict:
        _add_reason(email, f"Rule-based spam/injection signals disagree with {cat.name}")
    if out.red_flags:
        flags = ", ".join(sorted(f.value for f in out.red_flags))
        _add_reason(email, f"LLM reported red flags: {flags}")
    if prompt.truncated:
        _add_reason(email, "Email was truncated before classification")

    audit.record(
        db,
        EventType.CLASSIFIED,
        email=email,
        model_name=row.model_name,
        details={
            "category": cat.name,
            "raw_category": out.category[:100],
            "confidence": out.confidence,
            "priority": out.priority.value,
            "low_confidence": low_conf,
            "unknown_category": unknown,
            "signal_conflict": conflict,
            "red_flags": [f.value for f in out.red_flags],
            "llm_attempts": result.attempts,
            "duration_ms": result.duration_ms,
            "prompt_version": CLASSIFY_PROMPT_VERSION,
        },
    )
    transition(db, email, EmailStatus.CLASSIFIED)
    db.commit()
    return row
