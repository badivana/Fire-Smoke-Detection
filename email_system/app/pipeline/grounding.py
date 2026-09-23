"""Deterministic anti-hallucination check for extracted data (spec rule 2).

Every extracted value must be traceable to the text the model was shown (subject, body,
attachment text). Values that are not found are replaced by null (items with an
untraceable name are dropped) and reported, so the admin sees what was removed.

Kinds of checks:
  literal - amounts, IDs: must appear in the source after case/space/comma folding
            ("INR 12,00,000" matches "inr 1200000"; "Rs 12 lakh" does not)
  date    - literal, or an ISO date (YYYY-MM-DD) whose day-first spelling is in the source;
            the value is then replaced by the source's own spelling. Month-first numeric
            forms are never assumed (03-04 is ambiguous).
  amount  - money values: every number in the value (e.g. "13,33,400.00" in
            "INR 13,33,400.00") must appear in the source, so a currency prefix from the body
            plus a figure from the PDF still matches; spaces inside a number (OCR "2,88, 156")
            are removed first. A value with no number is not an amount and is dropped.
  qty     - integers: must appear as a standalone number in the source
  text    - names, departments, terms: at least half of the value's words must appear
  file    - must be one of the email's attachment filenames
This is intentionally conservative: a correct value paraphrased by the model is dropped
and shows up as missing, which is safer than a confident invented one.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Any

_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_AMOUNT_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
# Only close a gap right after a thousands comma ("2,88, 156"); other spaces separate
# numbers (table cells like "20   52,000.00" must stay two numbers).
_DIGIT_GAP = re.compile(r"(?<=\d,)\s+(?=\d)")
_ISO = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})\s*$")

_FOLD_DROP = re.compile(r"[\s,]+")
_WORD = re.compile(r"[a-z0-9]+")

FIELD_KINDS: dict[str, dict[str, str]] = {
    "requirement": {
        "department": "text",
        "budget": "amount",
        "deadline": "date",
        "technical_specifications": "text",
    },
    "quotation": {
        "vendor": "text",
        "quotation_no": "literal",
        "taxes": "amount",
        "total": "amount",
        "validity": "text",
        "delivery_terms": "text",
        "attachment_filename": "file",
    },
    "invoice": {
        "vendor": "text",
        "invoice_no": "literal",
        "invoice_date": "date",
        "po_reference": "literal",
        "taxes": "amount",
        "total": "amount",
        "due_date": "date",
    },
}
ITEM_KINDS: dict[str, dict[str, str]] = {
    "requirement": {"name": "text", "quantity": "qty"},
    "quotation": {"name": "text", "qty": "qty", "unit_price": "amount"},
    "invoice": {"name": "text", "qty": "qty", "unit_price": "amount"},
}

# Fields a reply needs; empty ones are reported as missing information.
REQUIRED: dict[str, tuple[str, ...]] = {
    "requirement": ("department", "items", "budget", "deadline", "technical_specifications"),
    "quotation": ("vendor", "quotation_no", "items", "total", "validity", "delivery_terms"),
    "invoice": ("vendor", "invoice_no", "total", "due_date"),
}
ITEM_REQUIRED: dict[str, tuple[str, ...]] = {
    "requirement": ("quantity",),
    "quotation": ("qty", "unit_price"),
    "invoice": (),
}


def _fold(text: str) -> str:
    return _FOLD_DROP.sub("", unicodedata.normalize("NFKC", text).lower())


def _words(text: str) -> list[str]:
    return [w for w in _WORD.findall(unicodedata.normalize("NFKC", text).lower()) if len(w) >= 3]


@dataclass
class Source:
    text: str
    attachment_names: tuple[str, ...] = ()
    folded: str = field(init=False)
    amount_text: str = field(init=False)
    words: set[str] = field(init=False)

    def __post_init__(self) -> None:
        self.folded = _fold(self.text)
        norm = unicodedata.normalize("NFKC", self.text).lower()
        self.amount_text = _DIGIT_GAP.sub("", norm).replace(",", "")
        self.words = set(_words(self.text))

    def has_literal(self, value: str) -> bool:
        v = _fold(value)
        return bool(v) and v in self.folded

    def has_qty(self, value: int) -> bool:
        # Standalone number: not part of a larger number, ID, date or amount
        # ("INV-7781", "20-10-2026", "12,00,000" do not confirm 7781 / 20 / 12).
        pattern = rf"(?<![\w.,/-]){value}(?![\w/-]|[.,]\d)"
        return re.search(pattern, self.text) is not None

    def has_text(self, value: str) -> bool:
        words = _words(value)
        if not words:
            return self.has_literal(value)
        return sum(w in self.words for w in words) / len(words) >= 0.5

    def find_date(self, value: str) -> str | None:
        """Return the source's own spelling of an ISO date, if present (day-first only)."""
        m = _ISO.match(value)
        if not m:
            return None
        try:
            d = date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
        month = _MONTHS[d.month - 1]
        candidates = [f"{d.day:02d}{sep}{d.month:02d}{sep}{d.year}" for sep in "-/."]
        candidates += [f"{d.day}{sep}{d.month}{sep}{d.year}" for sep in "-/."]
        candidates += [
            f"{d.day} {month} {d.year}",
            f"{d.day} {month[:3]} {d.year}",
            f"{month} {d.day} {d.year}",
            f"{d.day}th {month} {d.year}",
        ]
        low = self.text.lower()
        for c in candidates:
            idx = low.find(c)
            if idx != -1 and not low[idx - 1 : idx].isdigit():
                return self.text[idx : idx + len(c)]
        return None

    def has_amount(self, value: str) -> bool:
        nums = _AMOUNT_NUM.findall(value)
        if not nums:
            return False
        for n in nums:
            plain = n.replace(",", "")
            if not re.search(rf"(?<![\d.]){re.escape(plain)}(?![\d])", self.amount_text):
                return False
        return True

    def resolve(self, kind: str, value: Any) -> tuple[bool, Any]:
        """(grounded?, value to keep). Dates may be restored to the source spelling;
        amounts lose whitespace inside numbers (OCR artefact)."""
        if value is None:
            return True, None
        if kind == "amount":
            cleaned = _DIGIT_GAP.sub("", str(value))
            return (True, cleaned) if self.has_amount(cleaned) else (False, None)
        if kind == "date":
            if self.has_literal(str(value)):
                return True, value
            found = self.find_date(str(value))
            return (True, found) if found else (False, None)
        ok = self.check(kind, value)
        return ok, (value if ok else None)

    def check(self, kind: str, value: Any) -> bool:
        if value is None:
            return True
        if kind == "qty":
            return self.has_qty(int(value))
        if kind == "file":
            return value in self.attachment_names
        if kind in ("date", "amount"):
            return self.resolve(kind, value)[0]
        if kind == "literal":
            return self.has_literal(str(value))
        return self.has_text(str(value))


@dataclass(frozen=True)
class GroundingResult:
    data: dict[str, Any]
    ungrounded: list[str]
    missing: list[str]


def ground(schema: str, data: dict[str, Any], source: Source) -> GroundingResult:
    data = {**data}
    ungrounded: list[str] = []
    for name, kind in FIELD_KINDS[schema].items():
        ok, value = source.resolve(kind, data.get(name))
        if not ok:
            ungrounded.append(name)
        data[name] = value

    kept = []
    for i, item in enumerate(data.get("items") or []):
        item = {**item}
        if not source.check("text", item.get("name")):
            ungrounded.append(f"items[{i}].name")
            continue  # an item we cannot trace is dropped entirely
        for name, kind in ITEM_KINDS[schema].items():
            if name == "name":
                continue
            ok, value = source.resolve(kind, item.get(name))
            if not ok:
                ungrounded.append(f"items[{i}].{name}")
            item[name] = value
        kept.append(item)
    data["items"] = kept

    missing: list[str] = [f for f in REQUIRED[schema] if data.get(f) in (None, "", [])]
    for sub in ITEM_REQUIRED[schema]:
        if any(it.get(sub) is None for it in kept):
            missing.append(sub)
    return GroundingResult(data=data, ungrounded=ungrounded, missing=missing)
