from datetime import UTC, datetime, timedelta, timezone

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm.exc import StaleDataError

from app.db import Base
from app.db.base import ApprovalDecision, DraftSource, EmailSource, EmailStatus
from app.db.models import (
    Approval,
    Attachment,
    AuditLog,
    Classification,
    Draft,
    Email,
    ProcessingError,
)
from app.db.session import make_engine
from tests.conftest import alembic_config

SPEC_TABLES = {
    "emails",
    "attachments",
    "classifications",
    "extractions",
    "drafts",
    "approvals",
    "audit_logs",
    "processing_errors",
}


def make_email(**kw) -> Email:
    defaults = dict(
        message_id="msg-1",
        source=EmailSource.DEMO,
        sender="hod.cs@college.edu",
        subject="Need 10 laptops",
        body_text="Please procure 10 laptops.",
        received_at=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
    )
    defaults.update(kw)
    return Email(**defaults)


def make_draft(email: Email, version: int = 1, **kw) -> Draft:
    defaults = dict(
        email=email,
        version=version,
        source=DraftSource.AI,
        subject="Re: x",
        body="Dear...",
        created_by="system",
        model_name="qwen3:4b",
    )
    defaults.update(kw)
    return Draft(**defaults)


# ------------------------------------------------------------------ migrations


def test_migration_creates_all_spec_tables(engine):
    assert SPEC_TABLES <= set(inspect(engine).get_table_names())


def test_models_and_migration_are_in_sync(engine):
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_downgrade_then_upgrade(db_url, engine):
    cfg = alembic_config(db_url)
    command.downgrade(cfg, "base")
    assert not (SPEC_TABLES & set(inspect(make_engine(db_url)).get_table_names()))
    command.upgrade(cfg, "head")
    assert SPEC_TABLES <= set(inspect(make_engine(db_url)).get_table_names())


def test_sqlite_foreign_keys_enabled(engine):
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1


# ------------------------------------------------------------------ integrity


def test_duplicate_message_id_rejected(db):
    db.add(make_email())
    db.commit()
    db.add(make_email(subject="same id again"))
    with pytest.raises(IntegrityError, match="message_id"):
        db.commit()


def test_attachment_requires_existing_email(db):
    db.add(
        Attachment(
            email_id=999,
            filename="q.pdf",
            mime_type="application/pdf",
            size_bytes=10,
            sha256="0" * 64,
        )
    )
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        db.commit()


def test_invalid_status_rejected_by_db(db):
    db.add(make_email())
    db.commit()
    with pytest.raises(IntegrityError, match="CHECK"):
        db.execute(text("UPDATE emails SET status='HACKED'"))


def test_invalid_status_rejected_by_orm(db):
    db.add(make_email(status="HACKED"))
    with pytest.raises(StatementError):
        db.commit()


def test_sent_requires_sent_at(db):
    db.add(make_email(status=EmailStatus.SENT))
    with pytest.raises(IntegrityError, match="sent_has_timestamp"):
        db.commit()


def test_confidence_must_be_between_0_and_1(db):
    e = make_email()
    db.add(
        Classification(
            email=e,
            category="REQUIREMENT",
            confidence=1.5,
            priority="LOW",
            requires_action=True,
            model_name="m",
            prompt_version="v1",
        )
    )
    with pytest.raises(IntegrityError, match="confidence_range"):
        db.commit()


def test_draft_cannot_skip_human_review(db):
    db.add(make_draft(make_email(), requires_human_review=False))
    with pytest.raises(IntegrityError, match="always_human_review"):
        db.commit()


def test_only_one_current_draft_per_email(db):
    e = make_email()
    db.add_all([make_draft(e, 1), make_draft(e, 2)])
    with pytest.raises(IntegrityError, match="UNIQUE"):
        db.commit()


def test_draft_versioning(db):
    e = make_email()
    d1 = make_draft(e, 1)
    db.add(d1)
    db.commit()
    d1.is_current = False
    db.flush()
    db.add(
        make_draft(
            e,
            2,
            source=DraftSource.ADMIN_EDIT,
            parent_draft_id=d1.id,
            model_name=None,
            created_by="admin",
        )
    )
    db.commit()
    db.refresh(e)
    assert [d.version for d in e.drafts] == [1, 2]
    assert e.current_draft.version == 2
    assert e.current_draft.source == DraftSource.ADMIN_EDIT


def test_approved_decision_must_reference_draft(db):
    db.add(Approval(email=make_email(), decision=ApprovalDecision.APPROVED, approver="admin"))
    with pytest.raises(IntegrityError, match="approved_has_draft"):
        db.commit()


def test_approver_cannot_be_blank(db):
    db.add(Approval(email=make_email(), decision=ApprovalDecision.REJECTED, approver="   "))
    with pytest.raises(IntegrityError, match="approver_not_blank"):
        db.commit()


# ------------------------------------------------------------------ delete rules


def test_pipeline_outputs_cascade_with_email(db):
    e = make_email()
    db.add_all(
        [
            e,
            Attachment(
                email=e,
                filename="a.pdf",
                mime_type="application/pdf",
                size_bytes=1,
                sha256="0" * 64,
            ),
            make_draft(e),
        ]
    )
    db.commit()
    db.delete(e)
    db.commit()
    assert db.execute(text("SELECT count(*) FROM attachments")).scalar() == 0
    assert db.execute(text("SELECT count(*) FROM drafts")).scalar() == 0


@pytest.mark.parametrize("evidence", ["audit", "error"])
def test_email_with_audit_evidence_cannot_be_deleted(db, evidence):
    e = make_email()
    db.add(e)
    if evidence == "audit":
        db.add(AuditLog(email=e, event_type="EMAIL_RECEIVED", actor="system"))
    else:
        db.add(
            ProcessingError(email=e, stage="classify", error_code="LLM_UNAVAILABLE", retryable=True)
        )
    db.commit()
    db.delete(e)
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        db.commit()


# ------------------------------------------------------------------ time + concurrency


def test_datetimes_round_trip_as_utc(db, session_factory):
    ist = timezone(timedelta(hours=5, minutes=30))
    db.add(make_email(received_at=datetime(2026, 9, 1, 15, 30, tzinfo=ist)))
    db.commit()
    with session_factory() as s2:
        got = s2.query(Email).one()
        assert got.received_at == datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
        assert got.received_at.tzinfo is not None
        assert got.created_at.tzinfo is not None


def test_naive_datetime_rejected(db):
    db.add(make_email(received_at=datetime(2026, 9, 1, 10, 0)))
    with pytest.raises(StatementError, match="naive datetime"):
        db.commit()


def test_concurrent_status_change_detected(db, session_factory):
    """Two admins acting on the same email: the second write must fail, not overwrite."""
    db.add(make_email(status=EmailStatus.UNDER_REVIEW))
    db.commit()
    with session_factory() as a, session_factory() as b:
        ea, eb = a.get(Email, 1), b.get(Email, 1)
        ea.status = EmailStatus.APPROVED
        a.commit()
        eb.status = EmailStatus.REJECTED
        with pytest.raises(StaleDataError):
            b.commit()
    db.expire_all()
    assert db.get(Email, 1).status == EmailStatus.APPROVED
