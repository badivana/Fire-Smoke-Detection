"""POST /gmail/sync: fetch new Gmail messages into the queue (not available in demo mode)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import admin_name, get_db, require_admin
from app.core.config import get_settings

router = APIRouter(prefix="/gmail", tags=["gmail"], dependencies=[Depends(require_admin)])


class SyncOut(BaseModel):
    listed: int
    created: int
    duplicates: int
    failed: int


def get_gmail_service():
    from app.gmail.auth import build_service, load_credentials

    return build_service(load_credentials(get_settings()))  # GMAIL_AUTH_FAILED -> 502


@router.post("/sync", response_model=SyncOut)
def sync(
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[str, Depends(admin_name)],
) -> SyncOut:
    settings = get_settings()
    if settings.demo_mode:
        raise HTTPException(status.HTTP_409_CONFLICT, "Gmail sync is disabled in DEMO_MODE")
    from app.gmail.sync import sync_gmail

    r = sync_gmail(db, get_gmail_service_dep(), settings)
    return SyncOut(
        listed=r.listed, created=r.created, duplicates=r.duplicates, failed=len(r.failed)
    )


def get_gmail_service_dep():
    """Indirection so tests can swap the service."""
    return get_gmail_service()
