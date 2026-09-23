"""Runs the migrations against a real PostgreSQL. Skipped unless TEST_POSTGRES_URL is set,
e.g. TEST_POSTGRES_URL=postgresql+psycopg://postgres@localhost/email_ai_test"""

import os
from datetime import UTC, datetime

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.db.session import make_engine
from tests.conftest import alembic_config

PG_URL = os.environ.get("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="TEST_POSTGRES_URL not set")


@pytest.fixture
def pg_engine():
    cfg = alembic_config(PG_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    eng = make_engine(PG_URL)
    yield eng
    eng.dispose()
    command.downgrade(cfg, "base")


def test_postgres_schema_and_constraints(pg_engine):
    assert {"emails", "drafts", "approvals", "audit_logs"} <= set(
        inspect(pg_engine).get_table_names()
    )
    now = datetime.now(UTC)
    with pg_engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO emails (message_id, source, sender, recipients, subject, body_text, "
                "received_at, status, needs_manual_review, review_reasons, spam_signals, "
                "processing_attempts, updated_at, version, created_at) VALUES "
                "('m1','demo','a@b.c','[]','s','b',:t,'NEW',false,'[]','[]',0,:t,1,:t)"
            ),
            {"t": now},
        )
    draft_sql = (
        "INSERT INTO drafts (email_id, version, source, subject, body, missing_information, "
        "reason_for_reply, requires_human_review, is_current, created_by, created_at) VALUES "
        "(1, :v, 'ai', 's', 'b', '[]', '', :hr, true, 'system', :t)"
    )
    with pytest.raises(IntegrityError, match="always_human_review"), pg_engine.begin() as c:
        c.execute(text(draft_sql), {"v": 1, "hr": False, "t": now})
    with pg_engine.begin() as c:
        c.execute(text(draft_sql), {"v": 1, "hr": True, "t": now})
    with pytest.raises(IntegrityError, match="one_current"), pg_engine.begin() as c:
        c.execute(text(draft_sql), {"v": 2, "hr": True, "t": now})
    with pytest.raises(IntegrityError, match="email_status"), pg_engine.begin() as c:
        c.execute(text("UPDATE emails SET status='HACKED'"))
