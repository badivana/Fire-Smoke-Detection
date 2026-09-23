import pytest
from sqlalchemy import select

from app.core.errors import ErrorCode, PipelineError
from app.db.base import DraftSource, EmailStatus
from app.db.models import AuditLog, Draft, Email, ProcessingError
from app.demo.samples import load_samples
from app.ingestion.service import ingest_email
from app.pipeline.draft import generate_draft
from app.pipeline.draft_checks import check_draft, ensure_signature, normalise_subject
from app.pipeline.process import process_email
from app.workflow.states import transition
from tests.fakes import FakeProvider, assert_nothing_sent, classification

SIG = "IT/Admin Office"


def draft_out(**kw):
    base = {
        "subject": "Re: x",
        "body": f"Dear sir,\nThank you.\n\n{SIG}",
        "missing_information": [],
        "reason_for_reply": "Acknowledge.",
        "requires_human_review": True,
    }
    return {**base, **kw}


def req_extract(**kw):
    base = {
        "department": None,
        "request_type": "procurement",
        "items": [],
        "budget": None,
        "deadline": None,
        "technical_specifications": None,
        "missing_information": [],
    }
    return {**base, **kw}


def ingest(db, sample_id) -> Email:
    return db.get(Email, ingest_email(db, load_samples()[sample_id].to_incoming()).email_id)


def events(db, email):
    return [
        a.event_type
        for a in db.scalars(
            select(AuditLog).where(AuditLog.email_id == email.id).order_by(AuditLog.id)
        )
    ]


# ------------------------------------------------------------------ full pipeline


def test_process_requirement_end_to_end_to_review(db):
    email = ingest(db, "requirement_missing_specs")
    llm = FakeProvider(
        [
            classification("REQUIREMENT", 0.9),
            req_extract(
                department="Civil Dept office", items=[{"name": "projectors", "quantity": None}]
            ),
            draft_out(
                subject="Re: projectors needed",
                body="Thank you. Please share:\n- quantity\n- budget\n\nIT/Admin Office",
                missing_information=["quantity", "budget"],
            ),
        ]
    )
    status = process_email(db, email, llm)
    assert status == EmailStatus.UNDER_REVIEW
    d = email.current_draft
    assert (d.version, d.source, d.is_current, d.requires_human_review) == (
        1,
        DraftSource.AI,
        True,
        True,
    )
    assert d.prompt_version == "draft-v1" and d.model_name == "fake:fake-model"
    for m in ("quantity", "budget", "deadline", "technical_specifications"):
        assert m in d.missing_information  # computed missing info always carried over
    ev = events(db, email)
    assert (
        ev.index("CLASSIFIED")
        < ev.index("EXTRACTED")
        < ev.index("DRAFT_GENERATED")
        < ev.index("MOVED_TO_REVIEW")
    )
    states = [
        (a.from_status, a.to_status)
        for a in db.scalars(
            select(AuditLog).where(
                AuditLog.email_id == email.id, AuditLog.event_type == "STATE_CHANGED"
            )
        )
    ]
    assert states == [
        ("NEW", "CLASSIFIED"),
        ("CLASSIFIED", "DRAFT_GENERATED"),
        ("DRAFT_GENERATED", "UNDER_REVIEW"),
    ]
    assert_nothing_sent(db)


def test_draft_prompt_contains_extracted_data_as_untrusted(db):
    email = ingest(db, "requirement_lab_pcs")
    llm = FakeProvider([classification(), req_extract(budget="INR 12,00,000"), draft_out()])
    process_email(db, email, llm)
    call = llm.calls[2]
    assert call.schema_name == "reply_draft"
    assert "EXTRACTED DATA (derived from the email, untrusted)" in call.user
    assert '"budget": "INR 12,00,000"' in call.user
    assert "Never state or imply that anything is approved" in call.system
    assert call.schema["properties"]["requires_human_review"] == {"type": "boolean", "enum": [True]}


def test_spam_gets_no_draft_and_goes_to_review(db):
    email = ingest(db, "spam_newsletter")
    llm = FakeProvider([classification("IRRELEVANT", 0.97, "LOW", False)])
    assert process_email(db, email, llm) == EmailStatus.UNDER_REVIEW
    assert len(llm.calls) == 1  # no extraction, no drafting call
    assert db.scalars(select(Draft)).first() is None
    assert any("No reply recommended" in r for r in email.review_reasons)
    assert "NO_DRAFT_NEEDED" in events(db, email)
    assert_nothing_sent(db)


def test_admin_can_force_draft_for_spam_category(db):
    email = ingest(db, "spam_newsletter")
    process_email(db, email, FakeProvider([classification("IRRELEVANT", 0.9, "LOW", False)]))
    llm = FakeProvider([draft_out(body="Please remove us from your list.\n\nIT/Admin Office")])
    d = generate_draft(db, email, llm, actor="admin1", force=True)
    assert d.version == 1 and d.created_by == "admin1"
    assert email.status == EmailStatus.UNDER_REVIEW


def test_technical_query_drafted_without_extraction(db):
    email = ingest(db, "technical_query_wifi")
    llm = FakeProvider([classification("TECHNICAL_QUERY"), draft_out()])
    assert process_email(db, email, llm) == EmailStatus.UNDER_REVIEW
    assert len(llm.calls) == 2 and "EXTRACTED DATA" in llm.calls[1].user


# ------------------------------------------------------------------ regenerate


def test_regenerate_creates_new_version_with_admin_instructions(db):
    email = ingest(db, "requirement_lab_pcs")
    process_email(db, email, FakeProvider([classification(), req_extract(), draft_out()]))
    first = email.current_draft
    llm = FakeProvider([draft_out(body="Shorter reply.\n\nIT/Admin Office")])
    second = generate_draft(
        db, email, llm, actor="admin1", admin_instructions="Make it shorter and more formal."
    )
    db.refresh(first)
    assert (first.is_current, second.is_current, second.version) == (False, True, 2)
    assert second.parent_draft_id == first.id and second.created_by == "admin1"
    assert (
        "Instructions from the reviewing administrator (trusted): Make it shorter"
        in llm.calls[0].user
    )
    assert email.status == EmailStatus.UNDER_REVIEW
    assert len(db.scalars(select(Draft)).all()) == 2  # history kept


def test_cannot_draft_after_approval(db):
    email = ingest(db, "requirement_lab_pcs")
    process_email(db, email, FakeProvider([classification(), req_extract(), draft_out()]))
    transition(db, email, EmailStatus.APPROVED, actor="admin1")
    db.commit()
    with pytest.raises(PipelineError) as ei:
        generate_draft(db, email, FakeProvider([draft_out()]))
    assert ei.value.code == ErrorCode.INVALID_TRANSITION
    assert email.current_draft.version == 1


# ------------------------------------------------------------------ safety checks on drafts


def test_model_cannot_opt_out_of_human_review(db):
    email = ingest(db, "requirement_lab_pcs")
    bad = draft_out(requires_human_review=False)
    with pytest.raises(PipelineError) as ei:
        process_email(db, email, FakeProvider([classification(), req_extract(), bad, bad]))
    assert ei.value.code == ErrorCode.LLM_SCHEMA_INVALID
    db.expire_all()
    assert db.get(Email, email.id).status == EmailStatus.ERROR
    assert db.scalars(select(ProcessingError)).one().stage == "draft"
    assert_nothing_sent(db)


def test_invented_numbers_links_and_commitments_are_warned(db):
    email = ingest(db, "invoice_network")
    body = (
        "Your invoice INV-7781 for INR 1,34,520 has been approved and payment has been "
        "made on 25-10-2026 for INR 99,999. Details: https://pay.example/x or "
        "billing@evil.example\n\nIT/Admin Office"
    )
    llm = FakeProvider(
        [
            classification("INVOICE"),
            {
                "vendor": "NetCore",
                "invoice_no": "INV-7781",
                "invoice_date": None,
                "po_reference": None,
                "items": [],
                "taxes": None,
                "total": "INR 1,34,520",
                "due_date": None,
                "missing_information": [],
            },
            draft_out(body=body),
        ]
    )
    process_email(db, email, llm)
    w = " | ".join(email.current_draft.warnings)
    assert "25-10-2026" in w and "99,999" in w
    assert "1,34,520" not in w and "7781" not in w  # these are in the email
    assert "https://pay.example/x" in w and "billing@evil.example" in w
    assert "has been approved" in w and "payment has been made" in w
    assert email.needs_manual_review
    assert email.current_draft.body == body  # warnings never rewrite the text
    assert_nothing_sent(db)


def test_llm_down_while_drafting(db):
    email = ingest(db, "requirement_lab_pcs")
    llm = FakeProvider(
        [classification(), req_extract(), PipelineError(ErrorCode.LLM_UNAVAILABLE, "down")]
    )
    with pytest.raises(PipelineError):
        process_email(db, email, llm)
    db.expire_all()
    email = db.get(Email, email.id)
    assert email.status == EmailStatus.ERROR and email.current_draft is None
    assert_nothing_sent(db)


def test_process_stops_at_classification_error(db):
    email = ingest(db, "requirement_lab_pcs")
    llm = FakeProvider(["bad", "bad"])
    with pytest.raises(PipelineError):
        process_email(db, email, llm)
    assert len(llm.calls) == 2  # no extraction/draft calls after the failure
    assert_nothing_sent(db)


# ------------------------------------------------------------------ helpers


@pytest.mark.parametrize(
    "model_subject, expected",
    [
        ("Re: Invoice INV-7781", "Re: Invoice INV-7781"),
        ("Thanks for your invoice", "Re: Invoice INV-7781 received"),
        ("RE: multi\nline", "RE: multi line"),
    ],
)
def test_normalise_subject(model_subject, expected):
    assert normalise_subject(model_subject, "Invoice INV-7781 received") == expected


def test_signature_appended_once():
    assert ensure_signature("Hello.", SIG) == f"Hello.\n\n{SIG}"
    assert ensure_signature(f"Hello.\n\n{SIG}\n", SIG) == f"Hello.\n\n{SIG}"


def test_clean_draft_has_no_warnings():
    src = "Invoice INV-7781 total INR 1,34,520 due 20-10-2026"
    body = f"We received invoice INV-7781 (INR 1,34,520, due 20-10-2026).\n\n{SIG}"
    assert check_draft("Re: Invoice INV-7781", body, src, SIG) == []


def test_placeholder_and_markup_warned():
    w = check_draft("Re: x", "Dear [Your Name],\n**Thanks**", "", SIG)
    assert any("placeholder" in x for x in w) and any("markup" in x for x in w)


# ------------------------------------------------------------------ red flags block auto-drafts


def test_red_flagged_email_gets_no_automatic_draft_even_if_misclassified(db):
    """Measured with qwen3:4b: the plain injection was classified REQUIREMENT and the
    drafted reply repeated the attacker's 'pre-approved' claim. Red flags now block
    automatic drafting regardless of category."""
    email = ingest(db, "requirement_lab_pcs")  # content irrelevant; flags come from the LLM
    llm = FakeProvider(
        [classification("REQUIREMENT", 1.0, red_flags=["instructions_to_ai"]), req_extract()]
    )
    assert process_email(db, email, llm) == EmailStatus.UNDER_REVIEW
    assert len(llm.calls) == 2  # classify + extract, no drafting call
    assert email.current_draft is None
    assert any(
        "No draft generated because of red flags (instructions_to_ai)" in r
        for r in email.review_reasons
    )
    assert_nothing_sent(db)


def test_rule_signals_also_block_auto_draft(db):
    email = ingest(db, "prompt_injection_vendor")  # hidden text + injection phrases
    llm = FakeProvider(
        [
            classification("INVOICE", 0.99),
            {
                "vendor": None,
                "invoice_no": None,
                "invoice_date": None,
                "po_reference": None,
                "items": [],
                "taxes": None,
                "total": None,
                "due_date": None,
                "missing_information": [],
            },
        ]
    )
    process_email(db, email, llm)
    assert email.current_draft is None and email.status == EmailStatus.UNDER_REVIEW
    reason = next(r for r in email.review_reasons if r.startswith("No draft generated"))
    assert "hidden_html_text" in reason and "suspicious_phrase" in reason


def test_admin_can_still_request_draft_for_flagged_email(db):
    email = ingest(db, "requirement_lab_pcs")
    process_email(
        db, email, FakeProvider([classification(red_flags=["pressure_or_threat"]), req_extract()])
    )
    d = generate_draft(db, email, FakeProvider([draft_out()]), actor="admin1", force=True)
    assert d is not None and d.created_by == "admin1"


def test_pre_approved_claim_is_warned():
    w = check_draft("Re: x", "The email indicates a pre-approved order.", "pre-approved", SIG)
    assert any("pre-approved" in x for x in w)
