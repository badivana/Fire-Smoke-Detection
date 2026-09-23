"""Dashboard statistics."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.api.schemas import Stats
from app.core.config import get_settings
from app.db.base import EmailStatus
from app.db.models import Email

router = APIRouter(prefix="/dashboard", tags=["dashboard"], dependencies=[Depends(require_admin)])


@router.get("/stats", response_model=Stats)
def stats(db: Annotated[Session, Depends(get_db)]) -> Stats:
    by_status = {s.value: 0 for s in EmailStatus}
    for st, n in db.execute(select(Email.status, func.count()).group_by(Email.status)):
        by_status[EmailStatus(st).value] = n
    by_category = {
        (c or "UNCLASSIFIED"): n
        for c, n in db.execute(select(Email.category, func.count()).group_by(Email.category))
    }
    review = db.scalar(select(func.count()).where(Email.needs_manual_review.is_(True))) or 0
    return Stats(
        total=sum(by_status.values()),
        by_status=by_status,
        by_category=by_category,
        needs_manual_review=review,
        awaiting_review=by_status["UNDER_REVIEW"] + by_status["EDIT_REQUIRED"],
        errors=by_status["ERROR"] + by_status["FAILED"],
        sent=by_status["SENT"],
        send_mode=get_settings().send_mode.value,
    )
