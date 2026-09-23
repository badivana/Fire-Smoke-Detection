"""Shared API dependencies."""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterator

from fastapi import Header, HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_sessionmaker
from app.llm.base import LLMProvider
from app.llm.factory import build_provider
from app.sending.base import EmailSender
from app.sending.factory import build_sender


def get_db() -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session


def require_admin(x_admin_key: str | None = Header(default=None)) -> None:
    """If ADMIN_API_KEY is configured (always in prod), require a matching X-Admin-Key."""
    expected = get_settings().admin_api_key
    if expected is None:
        return
    if not x_admin_key or not secrets.compare_digest(
        x_admin_key.encode(), expected.get_secret_value().encode()
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing X-Admin-Key")


def require_demo_mode() -> None:
    if not get_settings().demo_mode:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "demo endpoints are disabled")


def admin_name(x_admin_name: str | None = Header(default=None)) -> str:
    """Who is acting. Required for every state-changing call so the audit trail always
    names a person. (Authentication itself is X-Admin-Key; see require_admin.)"""
    name = " ".join((x_admin_name or "").split())
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "X-Admin-Name header is required")
    return name


def get_llm_provider() -> LLMProvider:
    return build_provider()


def get_sender_factory() -> Callable[[], EmailSender]:
    """Lazy: the sender is only built (and SEND_MODE checked) when a send really happens,
    so e.g. retrying an LLM error still works while sending is disabled."""
    return build_sender  # raises SEND_DISABLED (HTTP 403) when sending is not allowed
