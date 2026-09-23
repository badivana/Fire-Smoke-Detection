"""Structured logging that never emits secrets or email bodies.

Use `log_event(EventType.X, email_id=..., **fields)` for pipeline events. Fields whose
names look sensitive are replaced with [REDACTED]; free text is scrubbed for tokens.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from enum import StrEnum
from typing import Any

LOGGER_NAME = "email_ai"
REDACTED = "[REDACTED]"

# Secret-like field names: redacted if the name CONTAINS any of these.
_SECRET_KEYS = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
)
# Content field names: redacted if any word of the name is one of these ("body",
# "body_text", "raw_html"...), except metadata about content ("prompt_version",
# "subject_chars", "body_length", "raw_category" are safe and useful in logs).
_CONTENT_WORDS = frozenset({"body", "content", "text", "html", "prompt", "diff", "attachment_data"})
_METADATA_SUFFIXES = ("version", "chars", "count", "length", "len", "given", "id", "category")
_SECRET_PATTERNS = (
    re.compile(r"(?i)bearer\s+[a-z0-9._\-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\bnvapi-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\bya29\.[A-Za-z0-9_\-]+"),  # Google OAuth access token
    re.compile(r"\b1//[A-Za-z0-9_\-]{20,}"),  # Google refresh token
    re.compile(r"(?i)(password|api[_-]?key|secret|token)\s*[=:]\s*\S+"),
)
_MAX_VALUE_LEN = 300


class EventType(StrEnum):
    EMAIL_RECEIVED = "EMAIL_RECEIVED"
    DUPLICATE_IGNORED = "DUPLICATE_IGNORED"
    CLASSIFIED = "CLASSIFIED"
    EXTRACTED = "EXTRACTED"
    DRAFT_GENERATED = "DRAFT_GENERATED"
    MOVED_TO_REVIEW = "MOVED_TO_REVIEW"
    NO_DRAFT_NEEDED = "NO_DRAFT_NEEDED"
    DRAFT_EDITED = "DRAFT_EDITED"
    EDIT_REQUESTED = "EDIT_REQUESTED"
    ADMIN_APPROVED = "ADMIN_APPROVED"
    ADMIN_REJECTED = "ADMIN_REJECTED"
    REOPENED = "REOPENED"
    RETRY_REQUESTED = "RETRY_REQUESTED"
    SEND_STARTED = "SEND_STARTED"
    EMAIL_SENT = "EMAIL_SENT"
    STATE_CHANGED = "STATE_CHANGED"
    ERROR = "ERROR"


def scrub_secrets(value: str) -> str:
    for pat in _SECRET_PATTERNS:
        value = pat.sub(REDACTED, value)
    return value


def scrub_text(value: str) -> str:
    value = scrub_secrets(value)
    if len(value) > _MAX_VALUE_LEN:
        value = value[:_MAX_VALUE_LEN] + "...[truncated]"
    return value


def _is_sensitive(key: str) -> bool:
    k = key.lower()
    if any(s in k for s in _SECRET_KEYS):
        return True
    words = k.replace("-", "_").split("_")
    if words[-1] in _METADATA_SUFFIXES:
        return False
    return k in _CONTENT_WORDS or any(w in _CONTENT_WORDS for w in words)


def redact(obj: Any, _key: str = "") -> Any:
    if _key and _is_sensitive(_key):
        return REDACTED
    if isinstance(obj, dict):
        return {k: redact(v, str(k)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return scrub_text(obj)
    if obj is None or isinstance(obj, (int, float, bool)):
        return obj
    return scrub_text(str(obj))


class _RedactingFormatter(logging.Formatter):
    def __init__(self, as_json: bool) -> None:
        super().__init__()
        self.as_json = as_json

    def format(self, record: logging.LogRecord) -> str:
        fields = redact(getattr(record, "fields", {}) or {})
        msg = scrub_text(record.getMessage())
        if self.as_json:
            payload = {
                "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
                "level": record.levelname,
                "msg": msg,
                **fields,
            }
            return json.dumps(payload, default=str)
        extras = " ".join(f"{k}={v}" for k, v in fields.items())
        ts = self.formatTime(record, "%H:%M:%S")
        return f"{ts} {record.levelname:<7} {msg}" + (f" | {extras}" if extras else "")


def setup_logging(level: str = "INFO", as_json: bool = False) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_RedactingFormatter(as_json))
    logger.addHandler(handler)
    logger.setLevel(level.upper())
    logger.propagate = False
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def log_event(event: EventType, level: int = logging.INFO, **fields: Any) -> None:
    get_logger().log(level, event.value, extra={"fields": {"event": event.value, **fields}})
