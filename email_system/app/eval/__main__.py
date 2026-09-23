"""Classification accuracy on the demo set, against a REAL LLM.

    python -m app.eval                      # model from .env (default qwen3:4b)
    python -m app.eval --model qwen3:8b     # compare another model
    python -m app.eval --runs 3             # repeat to check stability
    python -m app.eval --set holdout        # held-out emails (never used for tuning)
    python -m app.eval --set all

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
from app.db.migrate import upgrade_to_head
from app.db.models import Email
from app.db.session import make_engine
from app.demo.samples import Sample, load_samples
from app.ingestion.service import ingest_email
from app.llm.factory import build_provider
from app.pipeline.classify import classify_email

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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--model", help="override model name")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--set", default="demo", choices=["demo", "holdout", "all"])
    ap.add_argument("--samples", help="comma-separated sample ids (default: whole set)")
    ap.add_argument("--min-accuracy", type=float, default=0.8)
    ap.add_argument("-v", "--verbose", action="store_true", help="print the LLM's reasons")
    args = ap.parse_args()
    setup_logging("WARNING")

    samples = sample_set(args.set)
    ids = args.samples.split(",") if args.samples else list(samples)
    model = args.model or get_settings().llm_model_name
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
