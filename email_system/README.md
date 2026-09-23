# Institutional Email AI (human-in-the-loop)

This system reads, classifies and extracts details from institutional emails, then drafts replies.
**It never sends an email without explicit admin approval.**

Status: **Phase 3 of 13 (demo ingestion)**. See `docs/DESIGN_REVIEW.md` for the review of error and spam handling.

## Install (clean Mac, Apple Silicon, zsh)

```zsh
# 1. Tools
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"  # if no Homebrew
brew install python@3.12 tesseract ollama

# 2. Project
cd email_system
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env

# 3. LLM (needed from Phase 4)
brew services start ollama
ollama pull qwen3:4b

# 4. Database (creates data/app.db)
alembic upgrade head

# 5. Run
pytest
python -m app.demo                # load the 10 demo emails (safe to re-run: duplicates are ignored)
uvicorn app.main:app --reload     # then open http://127.0.0.1:8000/docs
```

## Layout

```
email_system/
├── app/
│   ├── main.py              FastAPI app + /health
│   ├── core/
│   │   ├── config.py        Settings (.env), safety validators, fixed Gmail scopes
│   │   ├── categories.py    Loads/validates config/categories.yaml
│   │   ├── errors.py        ErrorCode -> (ERROR|FAILED|NONE, retryable, message)
│   │   └── logging.py       Event logging with secret/body redaction
│   ├── api/
│   │   ├── deps.py          get_db, X-Admin-Key check, demo-mode guard
│   │   └── demo.py          GET /demo/samples, POST /demo/emails
│   ├── demo/
│   │   ├── samples.yaml     10 fictional sample emails + expected results
│   │   ├── fixtures/        quotation PDF (text layer) + scanned quotation (image only)
│   │   └── __main__.py      python -m app.demo
│   ├── ingestion/
│   │   ├── models.py        IncomingEmail (demo and Gmail both map to this)
│   │   ├── normalize.py     HTML->text, hidden-text split, invisible chars, truncation
│   │   ├── spam_signals.py  rule-based spam/phishing/injection hints
│   │   ├── storage.py       content-addressed attachment files, filename sanitising
│   │   └── service.py       ingest_email(): dedupe -> clean -> signals -> store -> audit
│   ├── services/audit.py    audit row + redacted log line per event
│   └── db/
│       ├── base.py          Base, UTC datetime type, status enums
│       ├── models.py        8 tables: emails, attachments, classifications, extractions,
│       │                    drafts, approvals, audit_logs, processing_errors
│       └── session.py       Engine (SQLite FK pragma on), sessions, db_ping
├── migrations/              Alembic (alembic.ini at project root)
├── scripts/make_demo_fixtures.py
├── config/categories.yaml   Categories + spam signals (add categories here, no code)
├── docs/DESIGN_REVIEW.md
├── tests/
├── .env.example
├── requirements.txt / requirements-dev.txt
└── pyproject.toml
```

## Adding a category

Add an entry to `config/categories.yaml` and restart. The file is validated at startup,
and the app will not start if it is invalid.

## Database

- The schema is changed **only** through Alembic. Never call `create_all()`.
  - After changing a model: `alembic revision --autogenerate -m "..."`, then review the file.
  - Then run `alembic upgrade head`.
- Switch to PostgreSQL: set `DATABASE_URL=postgresql+psycopg://user:pass@host/db` and
  `pip install "psycopg[binary]"`.
- Optional PostgreSQL test: `TEST_POSTGRES_URL=postgresql+psycopg://... pytest tests/test_postgres.py`

Rules enforced by the database itself (not just by the code):

| Rule | How |
|---|---|
| One row per Gmail message | `UNIQUE(emails.message_id)` |
| Status is one of the 10 spec states | `CHECK` constraint |
| Every draft requires human review | `CHECK (requires_human_review)` |
| At most one current draft per email | partial unique index |
| An approval points at the exact draft approved and has a named approver | `CHECK` constraints |
| `SENT` means `sent_at` is recorded | `CHECK` constraint |
| Audit logs, approvals and errors can't be deleted with their email | `FOREIGN KEY ... ON DELETE RESTRICT` |
| Two admins acting at once can't overwrite each other | optimistic lock (`emails.version`) |
| All timestamps are UTC | `UTCDateTime` type (rejects naive datetimes) |

## Demo data

`app/demo/samples.yaml` has 10 fictional emails:

| id | expected category | what it tests |
|---|---|---|
| requirement_lab_pcs | REQUIREMENT | complete request (qty, specs, budget, deadline) |
| requirement_missing_specs | REQUIREMENT | missing quantity/specs/budget/deadline |
| quotation_pdf | VENDOR_QUOTATION | PDF with a text layer |
| quotation_scanned | VENDOR_QUOTATION | scanned PDF, no text layer (OCR in Phase 10) |
| invoice_network | INVOICE | invoice details in the body |
| technical_query_wifi | TECHNICAL_QUERY | HTML-only email |
| general_meeting | GENERAL | meeting request |
| spam_newsletter | IRRELEVANT | bulk-mail headers |
| phishing_password | IRRELEVANT | credential phishing, look-alike domain, Reply-To mismatch |
| prompt_injection_vendor | IRRELEVANT | hidden-HTML injection + zero-width-char obfuscation |

- API: `GET /demo/samples`, `POST /demo/emails` (body `{}` = all samples,
  `{"sample_ids": [...]}`, or `{"custom": {"sender", "subject", "body"}}`).
- The demo endpoints return 404 when `DEMO_MODE=false`. They require `X-Admin-Key` when
  `ADMIN_API_KEY` is set.
- Each sample has a fixed message id, so posting it again returns `duplicate: true`.
