"""Loads demo samples from samples.yaml and turns them into IncomingEmail objects."""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.db.base import EmailSource
from app.ingestion.models import IncomingAttachment, IncomingEmail

DEMO_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = DEMO_DIR / "fixtures"


class SampleAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str
    mime_type: str
    fixture: str


class SampleEmail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sender: str
    to: list[str] = Field(default_factory=list)
    subject: str
    received_at: AwareDatetime
    body_text: str | None = None
    body_html: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    labels: list[str] = Field(default_factory=list)
    attachments: list[SampleAttachment] = Field(default_factory=list)


class Sample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    description: str
    expected: dict[str, Any]
    email: SampleEmail

    @property
    def message_id(self) -> str:
        # Fixed per sample, so inserting the same sample twice is a duplicate.
        return f"<demo-{self.id}@demo.local>"

    def to_incoming(self) -> IncomingEmail:
        e = self.email
        return IncomingEmail(
            message_id=self.message_id,
            thread_id=f"demo-thread-{self.id}",
            source=EmailSource.DEMO,
            sender=e.sender,
            to=e.to,
            subject=e.subject,
            body_text=e.body_text,
            body_html=e.body_html,
            received_at=e.received_at,
            headers=e.headers,
            labels=e.labels,
            attachments=[
                IncomingAttachment(
                    filename=a.filename,
                    mime_type=a.mime_type,
                    data=(FIXTURES_DIR / a.fixture).read_bytes(),
                )
                for a in e.attachments
            ],
        )


@lru_cache
def load_samples(path: Path = DEMO_DIR / "samples.yaml") -> dict[str, Sample]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    samples = [Sample.model_validate(s) for s in raw["samples"]]
    ids = [s.id for s in samples]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate sample ids in samples.yaml")
    return {s.id: s for s in samples}


def custom_incoming(sender: str, subject: str, body: str, received_at: datetime) -> IncomingEmail:
    return IncomingEmail(
        message_id=f"<demo-custom-{uuid4().hex}@demo.local>",
        source=EmailSource.DEMO,
        sender=sender,
        subject=subject,
        body_text=body,
        received_at=received_at,
    )
