"""Fetch new Gmail messages and ingest them (the same path as demo emails).

Uses format=raw and Python's `email` parser, so headers, HTML/plain bodies and
attachments are handled identically for demo and real mail. Dedupe is on the Gmail
message id. Nothing here can send.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ErrorCode, PipelineError
from app.core.logging import get_logger
from app.db.base import EmailSource
from app.db.models import Email
from app.ingestion.models import IncomingAttachment, IncomingEmail
from app.ingestion.service import ingest_email

MAX_RAW_BYTES = 30 * 1024 * 1024
MAX_ATTACHMENTS = 20
HEADERS_KEPT = ("reply-to", "list-unsubscribe", "list-id", "precedence", "auto-submitted")


@dataclass
class SyncResult:
    listed: int = 0
    created: int = 0
    duplicates: int = 0
    failed: list[str] = field(default_factory=list)


def _http_status(exc: Exception) -> int | None:
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    return int(status) if status is not None else None


def _gmail_error(exc: Exception, what: str) -> PipelineError:
    status = _http_status(exc)
    if status in (401, 403):
        return PipelineError(ErrorCode.GMAIL_AUTH_FAILED, f"{what}: HTTP {status}")
    name = type(exc).__name__
    if "RefreshError" in name or "Auth" in name:
        return PipelineError(ErrorCode.GMAIL_AUTH_FAILED, f"{what}: {name}")
    return PipelineError(
        ErrorCode.GMAIL_FETCH_FAILED, f"{what}: {'HTTP ' + str(status) if status else name}"
    )


def parse_raw(
    raw: bytes, *, gmail_id: str, thread_id: str | None, labels: list[str], internal_ms: int | None
) -> IncomingEmail:
    msg: EmailMessage = BytesParser(policy=policy.default).parsebytes(raw)  # type: ignore[assignment]
    try:
        received = parsedate_to_datetime(str(msg["date"])) if msg["date"] else None
        if received is not None and received.tzinfo is None:
            received = received.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        received = None
    if received is None:
        received = (
            datetime.fromtimestamp(internal_ms / 1000, UTC) if internal_ms else datetime.now(UTC)
        )

    plain = msg.get_body(preferencelist=("plain",))
    html = msg.get_body(preferencelist=("html",))
    body_text = plain.get_content() if plain is not None else None
    body_html = html.get_content() if html is not None else None

    attachments = []
    for part in list(msg.iter_attachments())[:MAX_ATTACHMENTS]:
        data = part.get_payload(decode=True) or b""
        attachments.append(
            IncomingAttachment(
                filename=part.get_filename() or "attachment",
                mime_type=part.get_content_type(),
                data=data,
            )
        )

    headers = {h: str(msg[h]) for h in HEADERS_KEPT if msg[h] is not None}
    to = [addr for _, addr in getaddresses([str(v) for v in msg.get_all("to", [])])]
    return IncomingEmail(
        message_id=gmail_id,
        thread_id=thread_id,
        rfc_message_id=(str(msg["message-id"]).strip() or None) if msg["message-id"] else None,
        source=EmailSource.GMAIL,
        sender=str(msg["from"] or ""),
        to=to,
        subject=str(msg["subject"] or ""),
        body_text=body_text,
        body_html=body_html,
        received_at=received,
        headers=headers,
        labels=labels,
        attachments=attachments,
    )


def sync_gmail(db: Session, service, settings: Settings, *, max_results: int = 50) -> SyncResult:
    """Ingest new messages matching GMAIL_QUERY. Per-message problems are recorded and
    skipped; an auth failure stops the sync (GMAIL_AUTH_FAILED)."""
    result = SyncResult()
    try:
        resp = (
            service.users()
            .messages()
            .list(userId="me", q=settings.gmail_query, maxResults=max_results)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001  googleapiclient raises many types
        raise _gmail_error(exc, "list messages") from None
    ids = [m["id"] for m in resp.get("messages", [])]
    result.listed = len(ids)
    for gid in ids:
        if db.scalar(select(Email.id).where(Email.message_id == gid)) is not None:
            result.duplicates += 1
            continue
        try:
            m = service.users().messages().get(userId="me", id=gid, format="raw").execute()
            raw = base64.urlsafe_b64decode(m["raw"].encode())
            if len(raw) > MAX_RAW_BYTES:
                raise ValueError("message too large")
            incoming = parse_raw(
                raw,
                gmail_id=gid,
                thread_id=m.get("threadId"),
                labels=m.get("labelIds", []),
                internal_ms=int(m["internalDate"]) if m.get("internalDate") else None,
            )
        except Exception as exc:  # noqa: BLE001
            err = _gmail_error(exc, "fetch message") if _http_status(exc) else None
            if err is not None and err.code == ErrorCode.GMAIL_AUTH_FAILED:
                raise err from None
            get_logger().warning(
                "gmail message skipped",
                extra={"fields": {"gmail_id": gid, "error": type(exc).__name__}},
            )
            result.failed.append(gid)
            continue
        r = ingest_email(db, incoming)
        result.created += r.created
        result.duplicates += r.duplicate
    return result
