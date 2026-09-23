"""Shared API dependencies."""

from __future__ import annotations

import secrets
from collections.abc import Iterator

from fastapi import Header, HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_sessionmaker


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
