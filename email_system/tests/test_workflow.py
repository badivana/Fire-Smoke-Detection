"""Spec tests 11-14 (edit, reject, approve+send, send failure) and the send guards."""

import email as email_lib

import pytest
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.errors import ErrorCode, PipelineError
from app.db.base import DraftSource, EmailStatus
from app.db.models import Approval, AuditLog, Email
from app.demo.samples import load_samples
from app.ingestion.service import ingest_email
from app.pipeline.process import process_email
from app.sending.base import SendOutcomeUnknown, SendRejected
from app.sending.factory import build_sender
from app.sending.simulated import SimulatedSender
from app.workflow import actions
from tests.fakes import FakeProvider, RecordingSender, assert_nothing_sent, classification

ADMIN = "Asha (IT Admin)"
SIG = "IT/Admin Office"


def draft_out(body=f"Thank you. We will review your request.\n\n{SIG}"):
    return {
        "subject": "Re: x",
        "body": body,
        "missing_information": [],
        "reason_for_reply": "Acknowledge.",
        "requires_human_review": True,
    }


def reviewed(db, sample_id="requirement_lab_pcs", category="REQUIREMENT") -> Email:
    """An email processed by the (fake) AI and waiting in UNDER_REVIEW with a draft."""
    e = db.get(Email, ingest_email(db, load_samples()[sample_id].to_incoming()).email_id)
    extract = {
        "department": None,
        "request_type": "procurement",
        "items": [],
        "budget": None,
        "deadline": None,
        "technical_specifications": None,
        "missing_information": [],
    }
    process_email(db, e, FakeProvider([classification(category), extract, draft_out()]))
    assert e.status == EmailStatus.UNDER_REVIEW and e.current_draft is not None
    return e


def approved(db) -> Email:
    e = reviewed(db)
    actions.approve(db, e, ADMIN, draft_id=e.current_draft.id)
    return e


def events(db, e):
    return [
        a.event_type
        for a in db.scalars(select(AuditLog).where(AuditLog.email_id == e.id).order_by(AuditLog.id))
    ]


# ---------------------------------------------------------------- spec 13: approve + send


def test_approve_then_send_simulated(db):
    e = reviewed(db)
    d = e.current_draft
    appr = actions.approve(db, e, ADMIN, draft_id=d.id, comment="ok")
    assert e.status == EmailStatus.APPROVED
    assert appr.approver == ADMIN and appr.draft_id == d.id
    assert appr.content_sha256 == actions.content_hash(d.subject, d.body)

    sender = SimulatedSender(get_settings().outbox_dir)
    msg_id = actions.send_email(db, e, ADMIN, sender)
    assert e.status == EmailStatus.SENT and e.sent_at is not None
    assert e.sent_provider_message_id == msg_id and e.send_started_at is None

    files = sender.sent_files()
    assert len(files) == 1
    eml = email_lib.message_from_bytes(files[0].read_bytes())
    assert eml["To"] == "hod.cse@greenfield-institute.example"
    assert eml["In-Reply-To"] == e.message_id and eml["Subject"] == d.subject
    assert d.body.splitlines()[0] in eml.get_payload()
    ev = events(db, e)
    assert ev.index("ADMIN_APPROVED") < ev.index("SEND_STARTED") < ev.index("EMAIL_SENT")
    sent_row = db.scalars(select(AuditLog).where(AuditLog.event_type == "EMAIL_SENT")).one()
    assert sent_row.actor == ADMIN and sent_row.details["approved_by"] == ADMIN


def test_cannot_send_twice(db):
    e = approved(db)
    s = RecordingSender()
    actions.send_email(db, e, ADMIN, s)
    with pytest.raises(PipelineError) as ei:
        actions.send_email(db, e, ADMIN, s)
    assert ei.value.code == ErrorCode.SEND_NOT_APPROVED and len(s.sent) == 1


@pytest.mark.parametrize(
    "status", [EmailStatus.UNDER_REVIEW, EmailStatus.REJECTED, EmailStatus.EDIT_REQUIRED]
)
def test_send_refused_unless_approved(db, status):
    e = reviewed(db)
    if status == EmailStatus.REJECTED:
        actions.reject(db, e, ADMIN)
    elif status == EmailStatus.EDIT_REQUIRED:
        actions.request_edit(db, e, ADMIN, comment="fix tone")
    s = RecordingSender()
    with pytest.raises(PipelineError) as ei:
        actions.send_email(db, e, ADMIN, s)
    assert ei.value.code == ErrorCode.SEND_NOT_APPROVED
    assert s.sent == [] and e.status == status
    assert_nothing_sent(db)


def test_send_refused_if_draft_changed_after_approval_even_directly_in_db(db):
    e = approved(db)
    db.execute(text("UPDATE drafts SET body = 'Pay us now to account 123' WHERE is_current"))
    db.commit()
    db.expire_all()
    e = db.get(Email, e.id)
    s = RecordingSender()
    with pytest.raises(PipelineError) as ei:
        actions.send_email(db, e, ADMIN, s)
    assert ei.value.code == ErrorCode.SEND_NOT_APPROVED and "changed after approval" in str(
        ei.value
    )
    assert s.sent == [] and e.status == EmailStatus.APPROVED
    assert_nothing_sent(db)


def test_send_disabled_mode_refuses(db, monkeypatch):
    e = approved(db)
    monkeypatch.setenv("SEND_MODE", "disabled")
    get_settings.cache_clear()
    with pytest.raises(PipelineError) as ei:
        build_sender()
    assert ei.value.code == ErrorCode.SEND_DISABLED
    assert e.status == EmailStatus.APPROVED
    assert_nothing_sent(db)


def test_gmail_sender_not_available_yet(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("SEND_MODE", "gmail")
    get_settings.cache_clear()
    with pytest.raises(PipelineError) as ei:
        build_sender()
    assert ei.value.code == ErrorCode.SEND_DISABLED


def test_system_actor_cannot_approve_or_send(db):
    e = reviewed(db)
    for name in ("system", "", "   ", "x" * 200):
        with pytest.raises(PipelineError) as ei:
            actions.approve(db, e, name, draft_id=e.current_draft.id)
        assert ei.value.code == ErrorCode.INVALID_ADMIN
    assert e.status == EmailStatus.UNDER_REVIEW


def test_approve_requires_the_draft_the_admin_saw(db):
    e = reviewed(db)
    first = e.current_draft
    actions.save_draft(db, e, "Colleague", subject="Re: x", body=f"Edited.\n\n{SIG}")
    with pytest.raises(PipelineError) as ei:
        actions.approve(db, e, ADMIN, draft_id=first.id)
    assert ei.value.code == ErrorCode.STALE_DRAFT
    assert e.status == EmailStatus.UNDER_REVIEW


def test_warnings_must_be_acknowledged(db):
    e = reviewed(db)
    actions.save_draft(
        db, e, ADMIN, subject="Re: x", body=f"Your order is approved, total INR 99,999.\n\n{SIG}"
    )
    d = e.current_draft
    assert d.warnings
    with pytest.raises(PipelineError) as ei:
        actions.approve(db, e, ADMIN, draft_id=d.id)
    assert ei.value.code == ErrorCode.WARNINGS_NOT_ACKNOWLEDGED
    appr = actions.approve(db, e, ADMIN, draft_id=d.id, acknowledge_warnings=True)
    assert appr.decision == "APPROVED"


def test_invalid_reply_address_refused(db):
    e = approved(db)
    e.sender = "not-an-address"
    db.commit()
    with pytest.raises(PipelineError) as ei:
        actions.send_email(db, e, ADMIN, RecordingSender())
    assert ei.value.code == ErrorCode.INVALID_RECIPIENT
    assert_nothing_sent(db)


# ---------------------------------------------------------------- spec 11: admin edits


def test_admin_edit_creates_version_and_diff_in_audit_not_logs(db, capsys):
    from app.core.logging import setup_logging

    setup_logging("INFO")
    e = reviewed(db)
    v1 = e.current_draft
    v2 = actions.save_draft(
        db,
        e,
        ADMIN,
        subject="Re: Requirement received",
        body=f"Dear Dr. Kulkarni,\nWe have noted UNIQUE-EDIT-TEXT.\n\n{SIG}",
        expected_draft_id=v1.id,
    )
    db.refresh(v1)
    assert (v2.version, v2.source, v2.created_by, v2.model_name) == (
        2,
        DraftSource.ADMIN_EDIT,
        ADMIN,
        None,
    )
    assert v2.parent_draft_id == v1.id and not v1.is_current and v2.is_current
    row = db.scalars(select(AuditLog).where(AuditLog.event_type == "DRAFT_EDITED")).one()
    assert row.actor == ADMIN
    assert "+We have noted UNIQUE-EDIT-TEXT." in row.details["diff"]
    assert "-Thank you. We will review your request." in row.details["diff"]
    assert "UNIQUE-EDIT-TEXT" not in capsys.readouterr().out  # diff never logged
    assert e.status == EmailStatus.UNDER_REVIEW


def test_edit_after_approval_withdraws_approval(db):
    e = approved(db)
    actions.save_draft(db, e, ADMIN, subject="Re: x", body=f"Changed.\n\n{SIG}")
    assert e.status == EmailStatus.UNDER_REVIEW
    with pytest.raises(PipelineError):
        actions.send_email(db, e, ADMIN, RecordingSender())
    actions.approve(db, e, ADMIN, draft_id=e.current_draft.id)  # re-approval works
    assert actions.send_email(db, e, ADMIN, RecordingSender()) == "rec-1"


def test_unchanged_save_keeps_version_and_approval(db):
    e = approved(db)
    d = e.current_draft
    same = actions.save_draft(db, e, ADMIN, subject=d.subject, body=d.body)
    assert same.id == d.id and e.status == EmailStatus.APPROVED


def test_stale_edit_refused(db):
    e = reviewed(db)
    old = e.current_draft.id
    actions.save_draft(db, e, "A", subject="Re: x", body=f"one\n\n{SIG}")
    with pytest.raises(PipelineError) as ei:
        actions.save_draft(db, e, "B", subject="Re: x", body=f"two\n\n{SIG}", expected_draft_id=old)
    assert ei.value.code == ErrorCode.STALE_DRAFT


def test_admin_can_write_draft_for_email_without_one(db):
    e = db.get(Email, ingest_email(db, load_samples()["spam_newsletter"].to_incoming()).email_id)
    process_email(db, e, FakeProvider([classification("IRRELEVANT", 0.9, "LOW", False)]))
    assert e.current_draft is None
    d = actions.save_draft(db, e, ADMIN, subject="Unsubscribe", body="Please unsubscribe us.")
    assert d.version == 1 and d.subject.startswith("Re: ")


# ---------------------------------------------------------------- spec 12: reject


def test_reject(db):
    e = reviewed(db)
    row = actions.reject(db, e, ADMIN, reason="Duplicate request")
    assert e.status == EmailStatus.REJECTED
    assert (row.decision, row.approver, row.comment) == ("REJECTED", ADMIN, "Duplicate request")
    assert "ADMIN_REJECTED" in events(db, e)
    with pytest.raises(PipelineError):
        actions.send_email(db, e, ADMIN, RecordingSender())
    assert_nothing_sent(db)


def test_reject_after_approval_and_reopen(db):
    e = approved(db)
    actions.reject(db, e, ADMIN)
    assert e.status == EmailStatus.REJECTED
    actions.reopen(db, e, ADMIN)
    assert e.status == EmailStatus.UNDER_REVIEW
    with pytest.raises(PipelineError):  # the old approval does not count any more
        actions.send_email(db, e, ADMIN, RecordingSender())
    assert_nothing_sent(db)


def test_request_edit_then_resubmit(db):
    e = reviewed(db)
    actions.request_edit(db, e, "Reviewer", comment="Add the room number")
    assert e.status == EmailStatus.EDIT_REQUIRED
    actions.save_draft(db, e, ADMIN, subject="Re: x", body=f"Room B-204 noted.\n\n{SIG}")
    assert e.status == EmailStatus.UNDER_REVIEW


# ---------------------------------------------------------------- spec 14: send failure


def test_send_rejected_by_provider_goes_to_failed_and_can_retry(db):
    e = approved(db)
    with pytest.raises(PipelineError) as ei:
        actions.send_email(db, e, ADMIN, RecordingSender(SendRejected("550 mailbox full")))
    assert ei.value.code == ErrorCode.SEND_FAILED
    db.expire_all()
    e = db.get(Email, e.id)
    assert e.status == EmailStatus.FAILED and e.send_started_at is None
    assert e.last_error_code == "SEND_FAILED"
    assert_nothing_sent(db)
    # confirmed rejection => a retry is allowed and uses the same approval
    ok = RecordingSender()
    actions.send_email(db, e, ADMIN, ok)
    assert e.status == EmailStatus.SENT and len(ok.sent) == 1


@pytest.mark.parametrize(
    "error", [SendOutcomeUnknown("timeout"), TimeoutError("read"), RuntimeError("crash")]
)
def test_unknown_send_outcome_blocks_any_resend(db, error):
    e = approved(db)
    with pytest.raises(PipelineError) as ei:
        actions.send_email(db, e, ADMIN, RecordingSender(error))
    assert ei.value.code == ErrorCode.SEND_DELIVERY_UNKNOWN
    db.expire_all()
    e = db.get(Email, e.id)
    assert e.status == EmailStatus.FAILED and e.send_started_at is not None
    again = RecordingSender()
    with pytest.raises(PipelineError) as ei2:
        actions.send_email(db, e, ADMIN, again)
    assert ei2.value.code == ErrorCode.SEND_DELIVERY_UNKNOWN and again.sent == []
    with pytest.raises(PipelineError):  # can't edit around it either
        actions.save_draft(db, e, ADMIN, subject="Re: x", body="new text")
    assert_nothing_sent(db)


def test_crash_after_marker_blocks_resend(db):
    """Simulate a process crash after the provider call: send_started_at is committed but
    nothing else. The next attempt must refuse (possible duplicate)."""
    from datetime import UTC, datetime

    e = approved(db)
    e.send_started_at = datetime.now(UTC)
    db.commit()
    s = RecordingSender()
    with pytest.raises(PipelineError) as ei:
        actions.send_email(db, e, ADMIN, s)
    assert ei.value.code == ErrorCode.SEND_DELIVERY_UNKNOWN and s.sent == []


# ---------------------------------------------------------------- regenerate / retry


def test_regenerate_withdraws_approval_and_creates_new_version(db):
    e = approved(db)
    llm = FakeProvider([draft_out(f"Regenerated.\n\n{SIG}")])
    d = actions.regenerate(db, e, ADMIN, llm, instructions="Be brief")
    assert d.version == 2 and e.status == EmailStatus.UNDER_REVIEW
    with pytest.raises(PipelineError):
        actions.send_email(db, e, ADMIN, RecordingSender())


def test_retry_after_llm_error_then_process_again(db):
    e = db.get(
        Email, ingest_email(db, load_samples()["requirement_lab_pcs"].to_incoming()).email_id
    )
    with pytest.raises(PipelineError):
        process_email(db, e, FakeProvider([PipelineError(ErrorCode.LLM_UNAVAILABLE, "x")]))
    assert e.status == EmailStatus.ERROR
    actions.retry_processing(db, e, ADMIN)
    assert e.status == EmailStatus.NEW
    extract = {
        "department": None,
        "request_type": "procurement",
        "items": [],
        "budget": None,
        "deadline": None,
        "technical_specifications": None,
        "missing_information": [],
    }
    process_email(db, e, FakeProvider([classification(), extract, draft_out()]))
    assert e.status == EmailStatus.UNDER_REVIEW
    assert "RETRY_REQUESTED" in events(db, e)
    assert_nothing_sent(db)


def test_non_retryable_error_refused(db):
    e = db.get(
        Email, ingest_email(db, load_samples()["requirement_lab_pcs"].to_incoming()).email_id
    )
    from app.pipeline.failures import record_failure

    record_failure(db, e, "attachments", PipelineError(ErrorCode.ATTACHMENT_TOO_LARGE, "x"))
    with pytest.raises(PipelineError) as ei:
        actions.retry_processing(db, e, ADMIN)
    assert ei.value.code == ErrorCode.NOT_RETRYABLE and e.status == EmailStatus.ERROR


def test_approvals_table_records_every_decision(db):
    e = reviewed(db)
    actions.request_edit(db, e, "R1", comment="shorter")
    actions.save_draft(db, e, "R1", subject="Re: x", body=f"Short.\n\n{SIG}")
    actions.approve(db, e, "R2", draft_id=e.current_draft.id)
    rows = db.scalars(select(Approval).where(Approval.email_id == e.id).order_by(Approval.id)).all()
    assert [(r.decision, r.approver) for r in rows] == [("EDIT_REQUIRED", "R1"), ("APPROVED", "R2")]
