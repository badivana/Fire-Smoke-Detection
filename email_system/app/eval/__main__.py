"""Classification accuracy on the demo set, against a REAL LLM.

    python -m app.eval                      # model from .env (default qwen3:4b)
    python -m app.eval --model qwen3:8b     # compare another model
    python -m app.eval --runs 3             # repeat to check stability
    python -m app.eval --set holdout        # held-out emails (never used for tuning)
    python -m app.eval --set all
    python -m app.eval --task extract --set all   # extraction quality + invented values
    python -m app.eval --task draft --set all -v  # full pipeline; prints every draft

Uses a throw-away SQLite DB in a temp dir; your data/app.db is not touched.
Nothing is ever sent. Exit code 1 if accuracy < --min-accuracy or a safety check fails.
"""

from __future__ import annotations

import argparse
import tempfile
import time
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.core.errors import PipelineError
from app.core.logging import setup_logging
from app.db.base import EmailStatus
from app.db.migrate import upgrade_to_head
from app.db.models import Email
from app.db.session import make_engine
from app.demo.samples import Sample, load_samples
from app.eval.scoring import score_extraction
from app.ingestion.service import ingest_email
from app.llm.factory import build_provider
from app.pipeline.classify import classify_email
from app.pipeline.extract import extract_email
from app.pipeline.process import process_email
from app.workflow.states import transition

HOLDOUT = Path(__file__).resolve().parent / "holdout.yaml"


def sample_set(name: str) -> dict[str, Sample]:
    demo, holdout = load_samples(), load_samples(HOLDOUT)
    return {"demo": demo, "holdout": holdout, "all": {**demo, **holdout}}[name]


def run_once(model: str | None, samples: dict[str, Sample], sample_ids: list[str]) -> list[dict]:
    settings = get_settings()
    provider = build_provider(settings, model=model)
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite:///{Path(tmp) / 'eval.db'}"
        upgrade_to_head(url)
        engine = make_engine(url)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        with Session() as db:
            for sid in sample_ids:
                s = samples[sid]
                r = ingest_email(db, s.to_incoming())
                email = db.get(Email, r.email_id)
                t0 = time.monotonic()
                row = {
                    "id": sid,
                    "expected": s.expected["category"],
                    "expect_review": s.expected.get("needs_manual_review", False),
                }
                try:
                    c = classify_email(db, email, provider)
                    row.update(
                        got=c.category,
                        conf=c.confidence,
                        attempts=c.llm_attempts,
                        review=email.needs_manual_review,
                        error=None,
                        reason=c.reason,
                    )
                except PipelineError as e:
                    row.update(
                        got="ERROR",
                        conf=None,
                        attempts=None,
                        review=email.needs_manual_review,
                        error=e.code.value,
                        reason="",
                    )
                row["secs"] = time.monotonic() - t0
                row["sent"] = email.sent_at is not None
                rows.append(row)
        engine.dispose()
    return rows


def run_extract_once(model: str | None, samples: dict[str, Sample], ids: list[str]) -> list[dict]:
    """Extraction only: the email is put in its EXPECTED category, so extraction quality is
    measured independently of classification mistakes."""
    provider = build_provider(get_settings(), model=model)
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite:///{Path(tmp) / 'eval.db'}"
        upgrade_to_head(url)
        engine = make_engine(url)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        with Session() as db:
            for sid in ids:
                s = samples[sid]
                if "extract" not in s.expected:
                    continue
                email = db.get(Email, ingest_email(db, s.to_incoming()).email_id)
                email.category = s.expected["category"]
                transition(db, email, EmailStatus.CLASSIFIED)
                db.commit()
                t0 = time.monotonic()
                row = {"id": sid, "secs": 0.0, "error": None, "checks": [], "ungrounded": []}
                try:
                    ext = extract_email(db, email, provider)
                    row["checks"] = score_extraction(
                        s.expected["extract"], ext.schema_name, ext.data, ext.missing_information
                    )
                    row["ungrounded"] = ext.ungrounded_fields
                    row["missing"] = ext.missing_information
                    row["data"] = ext.data
                except PipelineError as e:
                    row["error"] = e.code.value
                row["secs"] = time.monotonic() - t0
                row["sent"] = email.sent_at is not None
                rows.append(row)
        engine.dispose()
    return rows


def main_extract(args, samples: dict[str, Sample], ids: list[str], model: str) -> int:
    all_rows: list[dict] = []
    for run in range(1, args.runs + 1):
        rows = run_extract_once(args.model, samples, ids)
        all_rows += rows
        print(f"\n=== extraction run {run}/{args.runs}  set={args.set}  model={model} ===")
        for r in rows:
            passed = sum(ok for _, ok, _ in r["checks"])
            status = r["error"] or f"{passed}/{len(r['checks'])} checks"
            print(
                f"{r['id']:26} {status:14} removed(invented)={r['ungrounded'] or '-'}"
                f"  {r['secs']:.1f}s"
            )
            for name, ok, got in r["checks"]:
                if not ok or args.verbose:
                    print(f"     {'ok ' if ok else 'XX '}{name}: got {got}")
            if args.verbose and r.get("missing") is not None:
                print(f"     missing_information: {r['missing']}")
    checks = [ok for r in all_rows for _, ok, _ in r["checks"]]
    errors = sum(r["error"] is not None for r in all_rows)
    removed = sum(len(r["ungrounded"]) for r in all_rows)
    sent = sum(r["sent"] for r in all_rows)
    acc = sum(checks) / len(checks) if checks else 0.0
    print(f"\nfield checks passed: {sum(checks)}/{len(checks)} = {acc:.0%}   errors: {errors}")
    print(f"values removed as not found in email (model invented/reworded): {removed}")
    print(f"emails sent: {sent}")
    failed = acc < args.min_accuracy or errors > 0 or sent > 0
    print("RESULT:", "FAIL" if failed else "PASS", f"(min {args.min_accuracy:.0%})")
    return 1 if failed else 0


# How a draft may naturally refer to a missing field.
_FIELD_PHRASES = {
    "quotation_no": ("quotation number", "quotation no", "reference number"),
    "invoice_no": ("invoice number", "invoice no"),
    "taxes": ("tax", "gst"),
    "qty": ("quantit",),
    "quantity": ("quantit", "how many", "number of"),
    "unit_price": ("unit price", "price per", "rate"),
    "technical_specifications": ("specification",),
    "delivery_terms": ("delivery",),
    "validity": ("validity", "valid"),
    "due_date": ("due date",),
    "items": ("item", "product"),
    "total": ("total",),
    "budget": ("budget",),
    "deadline": ("deadline", "required by", "timeline", "date"),
    "department": ("department",),
    "vendor": ("vendor", "company"),
}


def _mentions(body: str, field: str) -> bool:
    low = body.lower()
    phrases = _FIELD_PHRASES.get(field, (field.replace("_", " ").lower(),))
    return any(p in low for p in phrases)


def run_draft_once(model: str | None, samples: dict[str, Sample], ids: list[str]) -> list[dict]:
    """Full pipeline with real classification; checks each resulting draft."""
    provider = build_provider(get_settings(), model=model)
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite:///{Path(tmp) / 'eval.db'}"
        upgrade_to_head(url)
        engine = make_engine(url)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        with Session() as db:
            for sid in ids:
                s = samples[sid]
                email = db.get(Email, ingest_email(db, s.to_incoming()).email_id)
                t0 = time.monotonic()
                row = {"id": sid, "error": None, "checks": [], "draft": None}
                try:
                    process_email(db, email, provider)
                except PipelineError as e:
                    row["error"] = e.code.value
                d = email.current_draft
                # Expected: no automatic draft for spam or for any email with red flags.
                spam = s.expected["category"] == "IRRELEVANT"
                c = row["checks"]
                c.append(("status_under_review", email.status == EmailStatus.UNDER_REVIEW, ""))
                row["draft"] = d
                if spam:
                    c.append(("no_draft_for_spam", d is None, "draft" if d else "none"))
                elif d is None:
                    c.append(("draft_created", False, "none"))
                else:
                    c.append(("no_warnings", not d.warnings, "; ".join(d.warnings)))
                    ext = email.extractions[-1] if email.extractions else None
                    for m in ext.missing_information if ext else []:
                        if m.startswith("attachment content"):
                            continue
                        c.append((f"asks_for:{m}", _mentions(d.body, m), ""))
                    sig = get_settings().reply_signature
                    c.append(("signature", sig.lower() in d.body.lower(), ""))
                row["secs"] = time.monotonic() - t0
                row["sent"] = email.sent_at is not None or email.status == EmailStatus.SENT
                row["review"] = email.review_reasons
                rows.append(row)
        engine.dispose()
    return rows


def main_draft(args, samples: dict[str, Sample], ids: list[str], model: str) -> int:
    rows = run_draft_once(args.model, samples, ids)
    print(f"\n=== draft run  set={args.set}  model={model} ===")
    for r in rows:
        passed = sum(ok for _, ok, _ in r["checks"])
        status = r["error"] or f"{passed}/{len(r['checks'])} checks"
        print(f"{r['id']:26} {status:14} {r['secs']:.1f}s")
        for name, ok, got in r["checks"]:
            if not ok:
                print(f"     XX {name} {got}")
        if args.verbose and r["draft"] is not None:
            d = r["draft"]
            print(f"     --- draft v{d.version}: {d.subject}   [review: {r['review']}]")
            for line in d.body.splitlines():
                print(f"     | {line}")
    checks = [ok for r in rows for _, ok, _ in r["checks"]]
    errors = sum(r["error"] is not None for r in rows)
    sent = sum(r["sent"] for r in rows)
    acc = sum(checks) / len(checks) if checks else 0.0
    print(
        f"\ndraft checks passed: {sum(checks)}/{len(checks)} = {acc:.0%}   errors: {errors}"
        f"   emails sent: {sent}"
    )
    failed = acc < args.min_accuracy or sent > 0
    print("RESULT:", "FAIL" if failed else "PASS", f"(min {args.min_accuracy:.0%})")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--model", help="override model name")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--task", default="classify", choices=["classify", "extract", "draft"])
    ap.add_argument("--set", default="demo", choices=["demo", "holdout", "all"])
    ap.add_argument("--samples", help="comma-separated sample ids (default: whole set)")
    ap.add_argument("--min-accuracy", type=float, default=0.8)
    ap.add_argument("-v", "--verbose", action="store_true", help="print the LLM's reasons")
    args = ap.parse_args()
    setup_logging("WARNING")

    samples = sample_set(args.set)
    ids = args.samples.split(",") if args.samples else list(samples)
    model = args.model or get_settings().llm_model_name
    if args.task == "extract":
        return main_extract(args, samples, ids, model)
    if args.task == "draft":
        return main_draft(args, samples, ids, model)
    all_rows: list[dict] = []
    for run in range(1, args.runs + 1):
        rows = run_once(args.model, samples, ids)
        all_rows += rows
        print(f"\n=== run {run}/{args.runs}  set={args.set}  model={model} ===")
        header = f"{'sample':26} {'expected':17} {'got':17} {'conf':>5} {'review':>7}"
        print(f"{header} {'try':>4} {'sec':>6}")
        for r in rows:
            ok = "ok " if r["got"] == r["expected"] else "XX "
            conf = f"{r['conf']:.2f}" if r["conf"] is not None else "-"
            print(
                f"{ok}{r['id']:23} {r['expected']:17} {r['got']:17} {conf:>5} "
                f"{'yes' if r['review'] else '-':>7} {r['attempts'] or '-':>4} {r['secs']:6.1f}"
                + (f"  [{r['error']}]" if r["error"] else "")
            )
            if args.verbose and r["reason"]:
                print(f"     reason: {r['reason']}")

    n = len(all_rows)
    correct = sum(r["got"] == r["expected"] for r in all_rows)
    errors = sum(r["error"] is not None for r in all_rows)
    missed_review = [r["id"] for r in all_rows if r["expect_review"] and not r["review"]]
    sent = [r["id"] for r in all_rows if r["sent"]]
    acc = correct / n if n else 0.0
    print(
        f"\naccuracy: {correct}/{n} = {acc:.0%}   errors: {errors}   "
        f"avg sec/email: {sum(r['secs'] for r in all_rows) / max(n, 1):.1f}"
    )
    print(f"flagged-for-review misses: {missed_review or 'none'}   emails sent: {len(sent)}")
    failed = acc < args.min_accuracy or bool(missed_review) or bool(sent)
    print("RESULT:", "FAIL" if failed else "PASS", f"(min accuracy {args.min_accuracy:.0%})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
