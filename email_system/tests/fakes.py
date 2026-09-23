"""Test doubles: a scripted LLM provider and a 'nothing was sent' assertion."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.base import EmailStatus
from app.db.models import AuditLog, Email
from app.llm.base import LLMProvider, LLMResponse


@dataclass
class Call:
    system: str
    user: str
    schema: dict[str, Any]
    schema_name: str


@dataclass
class FakeProvider(LLMProvider):
    """Returns scripted responses in order. An Exception item is raised instead."""

    responses: list[Any] = field(default_factory=list)
    name: str = "fake"
    model: str = "fake-model"
    calls: list[Call] = field(default_factory=list)

    def generate_json(self, *, system, user, schema, schema_name) -> LLMResponse:
        self.calls.append(Call(system, user, schema, schema_name))
        if not self.responses:
            raise AssertionError("FakeProvider: no scripted response left")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        text = item if isinstance(item, str) else json.dumps(item)
        return LLMResponse(text=text, model=self.model, duration_ms=5)


def classification(
    category="REQUIREMENT",
    confidence=0.93,
    priority="MEDIUM",
    requires_action=True,
    reason="Department requests equipment.",
    red_flags=(),
) -> dict:
    return {
        "category": category,
        "confidence": confidence,
        "reason": reason,
        "priority": priority,
        "requires_action": requires_action,
        "red_flags": list(red_flags),
    }


def assert_nothing_sent(db: Session) -> None:
    """Spec rule: every failure test must prove no email was sent."""
    db.expire_all()
    assert db.scalars(select(Email).where(Email.status == EmailStatus.SENT)).first() is None
    assert db.scalars(select(Email).where(Email.sent_at.is_not(None))).first() is None
    assert (
        db.scalars(select(Email).where(Email.sent_provider_message_id.is_not(None))).first() is None
    )
    assert db.scalars(select(AuditLog).where(AuditLog.event_type == "EMAIL_SENT")).first() is None
    outbox = get_settings().outbox_dir
    assert not (outbox.exists() and any(outbox.iterdir())), "simulated outbox is not empty"


class RecordingSender:
    """Sender double: records messages, or raises a scripted exception."""

    name = "recording"

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.sent: list = []

    def send(self, message) -> str:
        if self.error is not None:
            raise self.error
        self.sent.append(message)
        return f"rec-{len(self.sent)}"
