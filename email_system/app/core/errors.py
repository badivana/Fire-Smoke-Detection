"""Single error taxonomy for the pipeline.

Every failure maps to exactly one ErrorCode, which decides:
  - the side state the email moves to (ERROR for processing, FAILED for sending)
  - whether POST /emails/{id}/retry is allowed
Nothing in this module can cause a send; failures only ever stop the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ErrorCode(StrEnum):
    # Processing (-> ERROR)
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    LLM_TIMEOUT = "LLM_TIMEOUT"
    LLM_SCHEMA_INVALID = "LLM_SCHEMA_INVALID"
    ATTACHMENT_PARSE_FAILED = "ATTACHMENT_PARSE_FAILED"
    ATTACHMENT_TOO_LARGE = "ATTACHMENT_TOO_LARGE"
    OCR_UNAVAILABLE = "OCR_UNAVAILABLE"
    GMAIL_AUTH_FAILED = "GMAIL_AUTH_FAILED"
    GMAIL_FETCH_FAILED = "GMAIL_FETCH_FAILED"
    # Sending (-> FAILED)
    SEND_DISABLED = "SEND_DISABLED"
    SEND_NOT_APPROVED = "SEND_NOT_APPROVED"
    SEND_FAILED = "SEND_FAILED"
    # Ambiguous: request timed out after reaching Gmail, the mail MAY have gone out.
    SEND_DELIVERY_UNKNOWN = "SEND_DELIVERY_UNKNOWN"
    # Workflow
    INVALID_TRANSITION = "INVALID_TRANSITION"
    DUPLICATE_EMAIL = "DUPLICATE_EMAIL"


class FailureState(StrEnum):
    ERROR = "ERROR"
    FAILED = "FAILED"
    NONE = "NONE"  # rejected request; email state unchanged


@dataclass(frozen=True)
class ErrorPolicy:
    state: FailureState
    retryable: bool
    user_message: str


ERROR_POLICIES: dict[ErrorCode, ErrorPolicy] = {
    ErrorCode.LLM_UNAVAILABLE: ErrorPolicy(
        FailureState.ERROR, True, "LLM service unreachable. Start Ollama and retry."
    ),
    ErrorCode.LLM_TIMEOUT: ErrorPolicy(FailureState.ERROR, True, "LLM timed out. Retry."),
    ErrorCode.LLM_SCHEMA_INVALID: ErrorPolicy(
        FailureState.ERROR, True, "LLM output failed validation twice. Review manually."
    ),
    ErrorCode.ATTACHMENT_PARSE_FAILED: ErrorPolicy(
        FailureState.ERROR, True, "Attachment could not be read."
    ),
    ErrorCode.ATTACHMENT_TOO_LARGE: ErrorPolicy(
        FailureState.ERROR, False, "Attachment exceeds size limit; review manually."
    ),
    ErrorCode.OCR_UNAVAILABLE: ErrorPolicy(
        FailureState.ERROR, True, "Tesseract not installed; scanned attachment unread."
    ),
    ErrorCode.GMAIL_AUTH_FAILED: ErrorPolicy(
        FailureState.ERROR, True, "Gmail authorization failed. Re-run OAuth setup."
    ),
    ErrorCode.GMAIL_FETCH_FAILED: ErrorPolicy(FailureState.ERROR, True, "Gmail fetch failed."),
    ErrorCode.SEND_DISABLED: ErrorPolicy(
        FailureState.NONE, False, "Sending is disabled (SEND_MODE=disabled)."
    ),
    ErrorCode.SEND_NOT_APPROVED: ErrorPolicy(
        FailureState.NONE, False, "Only APPROVED emails can be sent."
    ),
    ErrorCode.SEND_FAILED: ErrorPolicy(
        FailureState.FAILED, True, "Gmail rejected the send. Nothing was delivered."
    ),
    # Not auto-retryable: a blind retry could deliver the reply twice.
    ErrorCode.SEND_DELIVERY_UNKNOWN: ErrorPolicy(
        FailureState.FAILED,
        False,
        "Send outcome unknown. Check the Gmail Sent folder before retrying.",
    ),
    ErrorCode.INVALID_TRANSITION: ErrorPolicy(
        FailureState.NONE, False, "Action not allowed in the current state."
    ),
    ErrorCode.DUPLICATE_EMAIL: ErrorPolicy(
        FailureState.NONE, False, "Email already ingested (same message_id)."
    ),
}


class PipelineError(Exception):
    """Raised by any pipeline stage. `detail` must not contain email bodies or secrets."""

    def __init__(self, code: ErrorCode, detail: str = "") -> None:
        self.code = code
        self.detail = detail[:500]
        super().__init__(f"{code.value}: {self.detail}" if detail else code.value)

    @property
    def policy(self) -> ErrorPolicy:
        return ERROR_POLICIES[self.code]
