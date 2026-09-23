"""Load all demo samples into the configured database.

alembic upgrade head
python -m app.demo
"""

from __future__ import annotations

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.session import get_sessionmaker
from app.demo.samples import load_samples
from app.ingestion.service import ingest_email


def main() -> None:
    settings = get_settings()
    if not settings.demo_mode:
        raise SystemExit("DEMO_MODE is false; refusing to load demo data.")
    setup_logging("WARNING")
    with get_sessionmaker()() as db:
        print(f"{'sample':28} {'id':>4}  {'result':9}  review  signals")
        for sample in load_samples().values():
            r = ingest_email(db, sample.to_incoming())
            result = "created" if r.created else "duplicate"
            review = "YES" if r.needs_manual_review else "-"
            print(f"{sample.id:28} {r.email_id:>4}  {result:9}  {review:6}  {len(r.spam_signals)}")


if __name__ == "__main__":
    main()
