import pytest

from app.pipeline.grounding import Source, ground

SRC = Source(
    text=(
        "The CSE department needs 1. Desktop PCs - 20 units, Intel i5, 16 GB RAM.\n"
        "Approved budget: INR 12,00,000 (including GST). Required by: 15 October 2026.\n"
        "Quotation QTN-2026-0412 grand total INR 13,33,400.00, valid for 30 days."
    ),
    attachment_names=("QTN-2026-0412.pdf",),
)


@pytest.mark.parametrize(
    "value, ok",
    [
        ("INR 12,00,000", True),
        ("12,00,000", True),
        ("inr 1200000", True),
        ("Rs 12 lakh", False),
        ("INR 15,00,000", False),
        ("15 October 2026", True),
        ("2026-10-15", False),
        ("QTN-2026-0412", True),
        ("QTN-2026-0413", False),
    ],
)
def test_literal_values(value, ok):
    assert SRC.check("literal", value) is ok


@pytest.mark.parametrize("value, ok", [(20, True), (2, False), (200, False), (1, True)])
def test_quantities_are_standalone_numbers(value, ok):
    assert SRC.check("qty", value) is ok


@pytest.mark.parametrize(
    "value, ok",
    [
        ("CSE department", True),
        ("Desktop PCs", True),
        ("Laptops", False),
        ("Mechanical Engineering", False),
        ("Intel i5 16 GB RAM desktop", True),
    ],
)
def test_text_overlap(value, ok):
    assert SRC.check("text", value) is ok


def test_filename_must_be_real_attachment():
    assert SRC.check("file", "QTN-2026-0412.pdf")
    assert not SRC.check("file", "invented.pdf")


def test_invented_values_removed_and_reported():
    data = {
        "department": "CSE department",
        "request_type": "procurement",
        "items": [
            {"name": "Desktop PCs", "quantity": 20},
            {"name": "Gaming chairs", "quantity": 5},
            {"name": "Desktop PCs spare", "quantity": 7},
        ],
        "budget": "INR 15,00,000",  # invented
        "deadline": "15 October 2026",
        "technical_specifications": "Intel i5, 16 GB RAM",
    }
    g = ground("requirement", data, SRC)
    assert g.data["budget"] is None
    assert [it["name"] for it in g.data["items"]] == ["Desktop PCs", "Desktop PCs spare"]
    assert g.data["items"][1]["quantity"] is None  # 7 is not in the email
    assert g.ungrounded == ["budget", "items[1].name", "items[2].quantity"]
    assert g.missing == ["budget", "quantity"]


def test_nothing_found_means_everything_missing():
    g = ground(
        "quotation",
        {
            "vendor": None,
            "quotation_no": None,
            "items": [],
            "taxes": None,
            "total": None,
            "validity": None,
            "delivery_terms": None,
            "attachment_filename": None,
        },
        SRC,
    )
    assert g.ungrounded == []
    assert g.missing == ["vendor", "quotation_no", "items", "total", "validity", "delivery_terms"]


@pytest.mark.parametrize(
    "text, value, ok",
    [
        ("Invoice INV-7781 due 20-10-2026", 20, False),
        ("Invoice INV-7781 due 20-10-2026", 7781, False),
        ("budget 12,00,000", 12, False),
        ("price 28.50 each", 28, False),
        ("switch x 4 @ INR 28,500", 4, True),
        ("Desktop PCs - 20 units", 20, True),
        ("(50) licences", 50, True),
    ],
)
def test_quantity_not_confirmed_by_ids_dates_or_amounts(text, value, ok):
    assert Source(text=text).check("qty", value) is ok


def test_prompt_metadata_cannot_confirm_values(db):
    """Regression: a random nonce like 'a10f' or a timestamp must not ground qty 10."""
    from app.db.models import Email
    from app.demo.samples import load_samples
    from app.ingestion.service import ingest_email
    from app.pipeline.extract import grounding_source

    email = db.get(
        Email, ingest_email(db, load_samples()["requirement_missing_specs"].to_incoming()).email_id
    )
    src = grounding_source(email, 12000, 20000)
    assert "<<<" not in src.text and "2026-09-16" not in src.text
    for q in (10, 16, 40, 2026):
        assert not src.check("qty", q)


@pytest.mark.parametrize(
    "text, value, expected",
    [
        ("Invoice date: 20-09-2026", "2026-09-20", "20-09-2026"),  # model reformatted
        ("Invoice date: 20-09-2026", "20-09-2026", "20-09-2026"),  # copied exactly
        ("due by 5/10/2026", "2026-10-05", "5/10/2026"),
        ("Required by: 15 October 2026.", "2026-10-15", "15 October 2026"),
        ("Required by: 15 Oct 2026", "2026-10-15", "15 Oct 2026"),
        ("Invoice date: 20-09-2026", "2026-09-21", None),  # different date
        ("dated 03-04-2026", "2026-03-04", None),  # month-first never assumed
        ("dated 125-10-2026", "2026-10-25", None),  # part of a larger number
        ("no date here", "2026-99-01", None),  # invalid ISO date
    ],
)
def test_dates_restored_to_source_spelling(text, value, expected):
    ok, kept = Source(text=text).resolve("date", value)
    assert kept == expected and ok is (expected is not None)


def test_ground_restores_reformatted_invoice_date():
    src = Source(text="NetCore Invoice No: INV-7781 Invoice date: 20-09-2026 due 20-10-2026")
    g = ground(
        "invoice",
        {
            "vendor": "NetCore",
            "invoice_no": "INV-7781",
            "invoice_date": "2026-09-20",
            "po_reference": None,
            "items": [],
            "taxes": None,
            "total": None,
            "due_date": "2026-10-20",
        },
        src,
    )
    assert g.data["invoice_date"] == "20-09-2026" and g.data["due_date"] == "20-10-2026"
    assert g.ungrounded == []


@pytest.mark.parametrize(
    "text, value, ok, kept",
    [
        # currency from the body + figure from the PDF (measured with qwen3:4b)
        (
            "Grand total: INR 13,33,400 ... GRAND TOTAL 13,33,400.00",
            "INR 13,33,400.00",
            True,
            "INR 13,33,400.00",
        ),
        ("TOTAL 2,88, 156", "2,88, 156", True, "2,88,156"),  # OCR gap removed
        ("TOTAL 2,88,156", "INR 2,88,156", True, "INR 2,88,156"),
        ("GST @ 18%: INR 20,520", "GST @ 18%: INR 20,520", True, "GST @ 18%: INR 20,520"),
        ("TOTAL 2,88,156", "INR 3,88,156", False, None),  # invented figure
        ("TOTAL 1,33,400.00", "13,33,400.00", False, None),  # not a prefix match
        ("TOTAL 12,000", "INR 2,000", False, None),  # tail of a larger number
        ("TOTAL 2,88,156", "two lakh", False, None),  # no number at all
    ],
)
def test_amounts(text, value, ok, kept):
    assert Source(text=text).resolve("amount", value) == (ok, kept)
