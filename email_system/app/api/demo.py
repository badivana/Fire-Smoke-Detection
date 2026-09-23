"""Demo endpoints (only when DEMO_MODE=true)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin, require_demo_mode
from app.demo.samples import custom_incoming, load_samples
from app.ingestion.models import IngestResult
from app.ingestion.service import ingest_email

router = APIRouter(
    prefix="/demo",
    tags=["demo"],
    dependencies=[Depends(require_demo_mode), Depends(require_admin)],
)


class CustomEmail(BaseModel):
    sender: str = Field(min_length=3, max_length=320)
    subject: str = Field(default="", max_length=998)
    body: str = Field(min_length=1, max_length=200_000)


class DemoInsertRequest(BaseModel):
    """Either `sample_ids` (omit for all samples) or a `custom` email."""

    sample_ids: list[str] | None = None
    custom: CustomEmail | None = None


class DemoInsertItem(IngestResult):
    sample_id: str | None


class SampleInfo(BaseModel):
    id: str
    description: str
    expected_category: str | None
    attachments: list[str]


@router.get("/samples", response_model=list[SampleInfo])
def list_samples() -> list[SampleInfo]:
    return [
        SampleInfo(
            id=s.id,
            description=s.description,
            expected_category=s.expected.get("category"),
            attachments=[a.filename for a in s.email.attachments],
        )
        for s in load_samples().values()
    ]


@router.post("/emails", response_model=list[DemoInsertItem], status_code=status.HTTP_201_CREATED)
def insert_demo_emails(
    db: Annotated[Session, Depends(get_db)], req: DemoInsertRequest | None = None
) -> list[DemoInsertItem]:
    req = req or DemoInsertRequest()
    if req.custom is not None:
        c = req.custom
        result = ingest_email(db, custom_incoming(c.sender, c.subject, c.body, datetime.now(UTC)))
        return [DemoInsertItem(sample_id=None, **result.model_dump())]

    samples = load_samples()
    ids = req.sample_ids if req.sample_ids is not None else list(samples)
    unknown = [i for i in ids if i not in samples]
    if unknown:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown sample ids: {unknown}")
    return [
        DemoInsertItem(sample_id=i, **ingest_email(db, samples[i].to_incoming()).model_dump())
        for i in ids
    ]
