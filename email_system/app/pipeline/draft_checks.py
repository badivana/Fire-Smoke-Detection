"""Deterministic checks on a generated draft. They add warnings; they never change what
the draft says (the only automatic edits are formatting: subject line, signature).

The reviewing admin sees every warning next to the draft. Checks:
  - numbers (2+ digits) that do not appear in the email or extracted data
  - links / email addresses that do not appear in the email
  - commitment language (approval, payment, order confirmation)
  - leftover markup or template placeholders
"""

from __future__ import annotations

import re
import unicodedata

_NUM = re.compile(r"\d[\d,./-]*\d|\d")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>()\"']+")
_EMAIL = re.compile(r"(?i)\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
_PLACEHOLDER = re.compile(r"\[(?:your|insert|name|date|todo)[^\]]*\]|\{\{.*?\}\}", re.I)
_MARKUP = re.compile(r"</?(?:p|br|div|html|body|b|i|strong)\b[^>]*>|\*\*|^#{1,6}\s", re.I | re.M)

COMMITMENT_PHRASES = (
    "has been approved",
    "is approved",
    "pre-approved",
    "already approved",
    "we approve",
    "we have approved",
    "sanctioned",
    "we accept your quotation",
    "we accept the quotation",
    "quotation is accepted",
    "order has been placed",
    "we will place the order",
    "we have placed",
    "order confirmed",
    "purchase order has been issued",
    "we will issue a purchase order",
    "payment has been made",
    "payment has been processed",
    "we will pay",
    "will be paid on",
    "has been paid",
    "payment will be released",
    "bank details have been updated",
    "we will update the bank",
    "issue has been resolved",
    "has been fixed",
)


def _fold(text: str) -> str:
    return re.sub(r"[\s,]+", "", unicodedata.normalize("NFKC", text).lower())


def check_draft(subject: str, body: str, source_text: str, signature: str) -> list[str]:
    warnings: list[str] = []
    source = _fold(source_text + "\n" + signature)
    text = f"{subject}\n{body}"

    unknown_numbers = []
    for n in _NUM.findall(text):
        digits = re.sub(r"\D", "", n)
        if len(digits) >= 2 and _fold(n) not in source and digits not in source:
            unknown_numbers.append(n)
    if unknown_numbers:
        warnings.append(
            "Numbers not found in the email: " + ", ".join(sorted(set(unknown_numbers)))
        )

    links = [u for u in _URL.findall(text) if _fold(u) not in source]
    if links:
        warnings.append("Links not found in the email: " + ", ".join(sorted(set(links))))
    addresses = [a for a in _EMAIL.findall(text) if _fold(a) not in source]
    if addresses:
        warnings.append(
            "Email addresses not found in the email: " + ", ".join(sorted(set(addresses)))
        )

    low = " ".join(text.lower().split())
    commitments = [p for p in COMMITMENT_PHRASES if p in low]
    if commitments:
        warnings.append("Commitment language (check before sending): " + ", ".join(commitments))
    if _PLACEHOLDER.search(text):
        warnings.append("Draft contains a template placeholder")
    if _MARKUP.search(text):
        warnings.append("Draft contains markup (HTML/markdown)")
    return warnings


def normalise_subject(model_subject: str, original_subject: str) -> str:
    """One line, capped, and clearly a reply to the original thread."""
    subject = " ".join(model_subject.split())[:300]
    if not subject.lower().startswith("re:"):
        subject = f"Re: {' '.join(original_subject.split())}"[:300]
    return subject


def ensure_signature(body: str, signature: str) -> str:
    body = body.rstrip()
    if signature.strip() and signature.strip().lower() not in body[-400:].lower():
        body = f"{body}\n\n{signature.strip()}"
    return body
