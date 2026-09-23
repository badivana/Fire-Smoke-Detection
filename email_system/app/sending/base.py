"""Sender interface. Senders only deliver; every safety check happens before them in
app/workflow/actions.py:send_email. A sender must raise SendRejected when the message
certainly was NOT sent, and SendOutcomeUnknown when it may have been."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class OutgoingEmail:
    to: str
    subject: str
    body: str
    in_reply_to: str | None  # original Message-ID, keeps the reply in the thread
    thread_id: str | None


class SendRejected(Exception):
    """Provider refused the message; nothing was delivered. Safe to retry."""


class SendOutcomeUnknown(Exception):
    """Request may have reached the provider (e.g. timeout). NOT safe to retry blindly."""


class EmailSender(ABC):
    name: str

    @abstractmethod
    def send(self, message: OutgoingEmail) -> str:
        """Deliver and return the provider's message id."""
