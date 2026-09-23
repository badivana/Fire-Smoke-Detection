"""API tests (FastAPI TestClient, fake LLM and sender). Every failure path asserts that
nothing was sent."""

import pytest

from app.api.deps import get_llm_provider, get_sender_factory
from app.core.config import get_settings
from app.core.errors import ErrorCode, PipelineError
from app.sending.base import SendRejected
from app.sending.simulated import SimulatedSender
from tests.fakes import FakeProvider, RecordingSender, assert_nothing_sent, classification

H = {"X-Admin-Name": "Asha"}
SIG = "IT/Admin Office"
EXTRACT = {
    "department": "CSE",
    "request_type": "procurement",
    "items": [],
    "budget": None,
    "deadline": None,
    "technical_specifications": None,
    "missing_information": [],
}
DRAFT = {
    "subject": "Re: x",
    "body": f"Thank you, we will review.\n\n{SIG}",
    "missing_information": [],
    "reason_for_reply": "ack",
    "requires_human_review": True,
}


@pytest.fixture
def api(client):
    """Client + controllable fake LLM + real SimulatedSender (tmp outbox)."""
    state = {"llm": FakeProvider([]), "sender": None}
    app = client.app
    app.dependency_overrides[get_llm_provider] = lambda: state["llm"]
    if state["sender"] is None:
        state["sender"] = SimulatedSender(get_settings().outbox_dir)
    app.dependency_overrides[get_sender_factory] = lambda: lambda: state["sender"]
    client.state = state
    return client


def insert(api, sample="requirement_lab_pcs") -> int:
    r = api.post("/demo/emails", json={"sample_ids": [sample]})
    assert r.status_code == 201
    return r.json()[0]["email_id"]


def to_review(api, sample="requirement_lab_pcs", category="REQUIREMENT") -> dict:
    eid = insert(api, sample)
    api.state["llm"].responses[:] = [classification(category), EXTRACT, DRAFT]
    r = api.post(f"/emails/{eid}/process", headers=H)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- queue + detail


def test_list_and_filters(api):
    for s in ("requirement_lab_pcs", "spam_newsletter", "invoice_network"):
        insert(api, s)
    r = api.get("/emails")
    assert r.status_code == 200 and r.json()["total"] == 3
    first = r.json()["items"][0]
    assert set(first) >= {
        "id",
        "sender",
        "subject",
        "category",
        "priority",
        "status",
        "received_at",
        "has_draft",
    }
    assert api.get("/emails", params={"status": "NEW"}).json()["total"] == 3
    assert api.get("/emails", params={"status": "SENT"}).json()["total"] == 0
    assert api.get("/emails", params={"q": "invoice"}).json()["total"] == 1
    assert api.get("/emails", params={"status": "BOGUS"}).status_code == 422
    assert api.get("/emails", params={"limit": 1}).json()["items"].__len__() == 1


def test_detail_contains_everything_the_dashboard_needs(api):
    d = to_review(api)
    assert d["status"] == "UNDER_REVIEW"
    assert d["ai_banner"] == "AI GENERATED \u2014 HUMAN REVIEW REQUIRED"
    assert d["classification"]["category"] == "REQUIREMENT"
    assert d["extraction"]["schema_name"] == "requirement"
    assert d["draft"]["requires_human_review"] is True and d["draft"]["version"] == 1
    assert "approve" in d["allowed_actions"] and "send" not in d["allowed_actions"]
    assert api.get("/emails/99999").status_code == 404


def test_audit_endpoint(api):
    d = to_review(api)
    events = [a["event_type"] for a in api.get(f"/emails/{d['id']}/audit").json()]
    for e in ("EMAIL_RECEIVED", "CLASSIFIED", "EXTRACTED", "DRAFT_GENERATED", "MOVED_TO_REVIEW"):
        assert e in events


def test_stats(api):
    to_review(api)
    insert(api, "spam_newsletter")
    s = api.get("/dashboard/stats").json()
    assert s["total"] == 2 and s["by_status"]["UNDER_REVIEW"] == 1 and s["by_status"]["NEW"] == 1
    assert s["awaiting_review"] == 1 and s["sent"] == 0 and s["send_mode"] == "simulated"


# ---------------------------------------------------------------- full happy path (spec 13)


def test_edit_approve_send_via_api(api):
    d = to_review(api)
    eid, draft_id = d["id"], d["draft"]["id"]
    r = api.put(
        f"/emails/{eid}/draft",
        headers=H,
        json={
            "subject": "Re: Requirement",
            "body": f"Edited text.\n\n{SIG}",
            "expected_draft_id": draft_id,
        },
    )
    assert r.status_code == 200 and r.json()["draft"]["version"] == 2
    new_id = r.json()["draft"]["id"]
    assert (
        api.post(f"/emails/{eid}/approve", headers=H, json={"draft_id": draft_id}).status_code
        == 409
    )  # stale version
    r = api.post(f"/emails/{eid}/approve", headers=H, json={"draft_id": new_id})
    assert r.status_code == 200 and r.json()["status"] == "APPROVED"
    assert r.json()["approvals"][-1]["approver"] == "Asha"
    r = api.post(f"/emails/{eid}/send", headers=H)
    assert r.status_code == 200 and r.json()["status"] == "SENT"
    assert len(api.state["sender"].sent_files()) == 1
    assert api.post(f"/emails/{eid}/send", headers=H).status_code == 409  # no double send


# ---------------------------------------------------------------- guards


def test_send_before_approval_is_409_and_nothing_sent(api, db):
    d = to_review(api)
    r = api.post(f"/emails/{d['id']}/send", headers=H)
    assert r.status_code == 409 and r.json()["error"] == "SEND_NOT_APPROVED"
    assert_nothing_sent(db)


def test_state_changes_require_admin_name(api, db):
    d = to_review(api)
    for method, path, body in [
        ("post", "approve", {"draft_id": d["draft"]["id"]}),
        ("post", "send", None),
        ("post", "reject", None),
        ("put", "draft", {"subject": "a", "body": "b"}),
        ("post", "process", None),
        ("post", "regenerate-draft", None),
    ]:
        r = getattr(api, method)(f"/emails/{d['id']}/{path}", json=body)
        assert r.status_code == 400 and "X-Admin-Name" in r.text, path
    r = api.post(
        f"/emails/{d['id']}/approve",
        headers={"X-Admin-Name": "system"},
        json={"draft_id": d["draft"]["id"]},
    )
    assert r.status_code == 400 and r.json()["error"] == "INVALID_ADMIN"
    assert api.get(f"/emails/{d['id']}").json()["status"] == "UNDER_REVIEW"
    assert_nothing_sent(db)


def test_admin_key_protects_every_route(api, monkeypatch):
    d = to_review(api)
    monkeypatch.setenv("ADMIN_API_KEY", "k" * 40)
    get_settings.cache_clear()
    for method, path in [
        ("get", "/emails"),
        ("get", f"/emails/{d['id']}"),
        ("get", f"/emails/{d['id']}/audit"),
        ("get", "/dashboard/stats"),
        ("post", f"/emails/{d['id']}/send"),
    ]:
        assert getattr(api, method)(path, headers=H).status_code == 401, path
    assert api.get("/emails", headers={**H, "X-Admin-Key": "k" * 40}).status_code == 200


def test_send_disabled_returns_403(api, db, monkeypatch):
    d = to_review(api)
    api.post(f"/emails/{d['id']}/approve", headers=H, json={"draft_id": d["draft"]["id"]})
    from app.sending.factory import build_sender

    api.app.dependency_overrides[get_sender_factory] = lambda: build_sender
    monkeypatch.setenv("SEND_MODE", "disabled")
    get_settings.cache_clear()
    r = api.post(f"/emails/{d['id']}/send", headers=H)
    assert r.status_code == 403 and r.json()["error"] == "SEND_DISABLED"
    assert api.get(f"/emails/{d['id']}").json()["status"] == "APPROVED"
    assert_nothing_sent(db)


def test_process_twice_is_conflict(api):
    d = to_review(api)
    r = api.post(f"/emails/{d['id']}/process", headers=H)
    assert r.status_code == 409 and r.json()["error"] == "INVALID_TRANSITION"


# ---------------------------------------------------------------- reject (spec 12)


def test_reject_and_reopen(api, db):
    d = to_review(api)
    r = api.post(f"/emails/{d['id']}/reject", headers=H, json={"reason": "not needed"})
    assert r.status_code == 200 and r.json()["status"] == "REJECTED"
    assert r.json()["allowed_actions"] == ["reopen"]
    assert api.post(f"/emails/{d['id']}/send", headers=H).status_code == 409
    r = api.post(f"/emails/{d['id']}/reopen", headers=H)
    assert r.json()["status"] == "UNDER_REVIEW"
    assert_nothing_sent(db)


# ---------------------------------------------------------------- LLM failures (spec 8, 15)


def test_llm_unavailable_returns_503_and_email_in_error(api, db):
    eid = insert(api)
    api.state["llm"].responses[:] = [PipelineError(ErrorCode.LLM_UNAVAILABLE, "down")]
    r = api.post(f"/emails/{eid}/process", headers=H)
    assert r.status_code == 503 and r.json()["error"] == "LLM_UNAVAILABLE"
    assert r.json()["retryable"] is True
    d = api.get(f"/emails/{eid}").json()
    assert d["status"] == "ERROR" and d["allowed_actions"] == ["retry"]
    assert_nothing_sent(db)
    # retry works even when sending is disabled (sender is lazy)
    from app.sending.factory import build_sender

    api.app.dependency_overrides[get_sender_factory] = lambda: build_sender
    api.state["llm"].responses[:] = [classification(), EXTRACT, DRAFT]
    r = api.post(f"/emails/{eid}/retry", headers=H)
    assert r.status_code == 200 and r.json()["status"] == "UNDER_REVIEW"


def test_malformed_llm_output_returns_502(api, db):
    eid = insert(api)
    api.state["llm"].responses[:] = ["{bad", "{bad"]
    r = api.post(f"/emails/{eid}/process", headers=H)
    assert r.status_code == 502 and r.json()["error"] == "LLM_SCHEMA_INVALID"
    assert api.get(f"/emails/{eid}").json()["status"] == "ERROR"
    assert_nothing_sent(db)


# ---------------------------------------------------------------- send failure (spec 14)


def test_send_failure_then_retry_via_api(api, db):
    d = to_review(api)
    api.post(f"/emails/{d['id']}/approve", headers=H, json={"draft_id": d["draft"]["id"]})
    api.state["sender"] = RecordingSender(SendRejected("550"))
    r = api.post(f"/emails/{d['id']}/send", headers=H)
    assert r.status_code == 502 and r.json()["error"] == "SEND_FAILED"
    detail = api.get(f"/emails/{d['id']}").json()
    assert detail["status"] == "FAILED" and "retry" in detail["allowed_actions"]
    assert_nothing_sent(db)
    api.state["sender"] = RecordingSender()
    r = api.post(f"/emails/{d['id']}/retry", headers=H)
    assert r.status_code == 200 and r.json()["status"] == "SENT"


def test_unknown_send_outcome_locks_email(api, db):
    d = to_review(api)
    api.post(f"/emails/{d['id']}/approve", headers=H, json={"draft_id": d["draft"]["id"]})
    api.state["sender"] = RecordingSender(TimeoutError("read timeout"))
    r = api.post(f"/emails/{d['id']}/send", headers=H)
    assert r.status_code == 409 and r.json()["error"] == "SEND_DELIVERY_UNKNOWN"
    detail = api.get(f"/emails/{d['id']}").json()
    assert detail["allowed_actions"] == [] and detail["send_started_at"] is not None
    assert api.post(f"/emails/{d['id']}/retry", headers=H).status_code == 409
    assert_nothing_sent(db)


def test_regenerate_via_api(api):
    d = to_review(api)
    api.state["llm"].responses[:] = [{**DRAFT, "body": f"Shorter.\n\n{SIG}"}]
    r = api.post(f"/emails/{d['id']}/regenerate-draft", headers=H, json={"instructions": "shorter"})
    assert r.status_code == 200 and r.json()["draft"]["version"] == 2
    assert [v["version"] for v in r.json()["draft_versions"]] == [1, 2]


def test_error_responses_never_leak_email_body(api):
    eid = insert(api)
    api.state["llm"].responses[:] = ["{bad", "{bad"]
    r = api.post(f"/emails/{eid}/process", headers=H)
    assert "Approved budget" not in r.text and "Traceback" not in r.text
