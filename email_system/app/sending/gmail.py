"""Gmail sender (users.messages.send). Only reached through workflow.send_email, after
every approval check has passed.

Error mapping decides whether a retry is safe:
  - auth failure before the request, HTTP 400/401/403/404/429  -> SendRejected (not sent)
  - timeouts, connection drops, HTTP 5xx, anything unexpected -> SendOutcomeUnknown
    (the message may have been accepted; resending is blocked until a human checks)
"""

from __future__ import annotations

import base64
from email.message import EmailMessage

from app.sending.base import EmailSender, OutgoingEmail, SendOutcomeUnknown, SendRejected

REJECTED_STATUSES = {400, 401, 403, 404, 429}


class GmailSender(EmailSender):
    name = "gmail"

    def __init__(self, service) -> None:
        self._service = service

    @staticmethod
    def build_raw(message: OutgoingEmail) -> str:
        m = EmailMessage()
        m["To"] = message.to
        m["Subject"] = message.subject
        if message.in_reply_to:
            m["In-Reply-To"] = message.in_reply_to
            m["References"] = message.in_reply_to
        m.set_content(message.body)
        return base64.urlsafe_b64encode(bytes(m)).decode()

    def send(self, message: OutgoingEmail) -> str:
        body = {"raw": self.build_raw(message)}
        if message.thread_id:
            body["threadId"] = message.thread_id
        try:
            resp = (
                self._service.users().messages().send(userId="me", body=body).execute(num_retries=0)
            )  # never retry a send automatically
        except Exception as exc:  # noqa: BLE001
            status = getattr(getattr(exc, "resp", None), "status", None)
            name = type(exc).__name__
            if status is not None and int(status) in REJECTED_STATUSES:
                raise SendRejected(f"HTTP {status}") from None
            if "RefreshError" in name:  # token refresh failed before any send request
                raise SendRejected(name) from None
            raise SendOutcomeUnknown(f"HTTP {status}" if status else name) from None
        msg_id = resp.get("id") if isinstance(resp, dict) else None
        if not msg_id:
            raise SendOutcomeUnknown("no message id in Gmail response")
        return str(msg_id)
