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
        # Implemented in Phase 11. Until then real sending is impossible.
        raise PipelineError(ErrorCode.SEND_DISABLED, "Gmail sending is not implemented yet")
    raise PipelineError(ErrorCode.SEND_DISABLED, "SEND_MODE=disabled")
