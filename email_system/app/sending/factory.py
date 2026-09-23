"""Select the sender for SEND_MODE. Anything unexpected fails closed (no sender)."""

from __future__ import annotations

from app.core.config import SendMode, Settings, get_settings
from app.core.errors import ErrorCode, PipelineError
from app.sending.base import EmailSender
from app.sending.simulated import SimulatedSender


def build_sender(settings: Settings | None = None) -> EmailSender:
    s = settings or get_settings()
    if s.send_mode == SendMode.SIMULATED:
        return SimulatedSender(s.outbox_dir)
    if s.send_mode == SendMode.GMAIL:
        from app.gmail.auth import build_service, load_credentials
        from app.sending.gmail import GmailSender

        # Raises GMAIL_AUTH_FAILED (nothing sent) if the token is missing/invalid.
        return GmailSender(build_service(load_credentials(s)))
    raise PipelineError(ErrorCode.SEND_DISABLED, "SEND_MODE=disabled")
