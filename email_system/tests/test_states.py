import pytest
from sqlalchemy import select

from app.core.errors import ErrorCode, PipelineError
from app.db.base import EmailStatus as S
from app.db.models import AuditLog
from app.workflow.states import ALLOWED, can_transition, transition
from tests.test_db import make_email


def test_every_state_has_rules():
    assert set(ALLOWED) == set(S)


def test_approved_only_from_under_review():
    sources = {s for s, targets in ALLOWED.items() if S.APPROVED in targets}
    assert sources == {S.UNDER_REVIEW}


def test_sent_only_after_approval():
    sources = {s for s, targets in ALLOWED.items() if S.SENT in targets}
    assert sources == {S.APPROVED, S.FAILED}
    # FAILED is only reachable from APPROVED, so every path to SENT passes APPROVED.
    assert {s for s, t in ALLOWED.items() if S.FAILED in t} == {S.APPROVED}


def test_sent_is_terminal():
    assert ALLOWED[S.SENT] == frozenset()


def test_no_path_to_sent_skips_under_review():
    """Graph check: remove UNDER_REVIEW and SENT must become unreachable from NEW."""
    seen, stack = set(), [S.NEW]
    while stack:
        s = stack.pop()
        if s in seen or s == S.UNDER_REVIEW:
            continue
        seen.add(s)
        stack.extend(ALLOWED[s])
    assert S.SENT not in seen and S.APPROVED not in seen


def test_transition_updates_and_audits(db):
    e = make_email()
    db.add(e)
    db.flush()
    transition(db, e, S.CLASSIFIED, details={"x": 1})
    db.commit()
    assert e.status == S.CLASSIFIED
    log = db.scalars(select(AuditLog)).one()
    assert (log.event_type, log.from_status, log.to_status) == (
        "STATE_CHANGED",
        "NEW",
        "CLASSIFIED",
    )


@pytest.mark.parametrize("target", [S.APPROVED, S.SENT, S.UNDER_REVIEW, S.REJECTED])
def test_invalid_transition_rejected_without_change(db, target):
    e = make_email()
    db.add(e)
    db.flush()
    with pytest.raises(PipelineError) as ei:
        transition(db, e, target)
    assert ei.value.code == ErrorCode.INVALID_TRANSITION
    assert e.status == S.NEW
    assert not can_transition(S.NEW, target)
