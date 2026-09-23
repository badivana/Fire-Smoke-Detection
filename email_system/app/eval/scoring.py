"""Compare an extraction with a sample's `expected.extract` block."""

from __future__ import annotations

import unicodedata
from typing import Any

from app.pipeline.grounding import REQUIRED

SPECIAL = {"items_count", "quantities", "missing_required", "missing_includes"}


def _fold(v: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(v)).lower().replace(",", "").split())


def _matches(expected: Any, got: Any) -> bool:
    if expected is None:
        return got in (None, "", [])
    if got in (None, "", []):
        return False
    options = expected if isinstance(expected, list) else [expected]
    return any(_fold(o) in _fold(got) for o in options)


def score_extraction(
    expected: dict[str, Any], schema: str, data: dict[str, Any], missing: list[str]
) -> list[tuple[str, bool, str]]:
    """Returns (check name, passed, what we got)."""
    checks: list[tuple[str, bool, str]] = []
    for key, exp in expected.items():
        if key in SPECIAL:
            continue
        got = data.get(key)
        checks.append((key, _matches(exp, got), repr(got)))
    items = data.get("items") or []
    if "items_count" in expected:
        checks.append(("items_count", len(items) == expected["items_count"], str(len(items))))
    if "quantities" in expected:
        qty_key = "quantity" if schema == "requirement" else "qty"
        got_q = [it.get(qty_key) for it in items]
        checks.append(("quantities", got_q == expected["quantities"], str(got_q)))
    folded_missing = [_fold(m) for m in missing]
    if "missing_includes" in expected:
        for m in expected["missing_includes"]:
            ok = any(_fold(m) in fm for fm in folded_missing)
            checks.append((f"missing:{m}", ok, str(missing)))
    if "missing_required" in expected:
        wrongly = [
            f
            for f in REQUIRED[schema]
            if f not in expected["missing_required"] and _fold(f) in folded_missing
        ]
        checks.append(("no_false_missing", not wrongly, str(wrongly)))
    return checks
