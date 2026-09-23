# Institutional Email AI (human-in-the-loop)

This system reads, classifies and extracts details from institutional emails, then drafts replies.
**It never sends an email without explicit admin approval.**

Status: **Phase 2 of 13 (database + migrations)**. See `docs/DESIGN_REVIEW.md` for the review of error and spam handling.

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
uvicorn app.main:app --reload     # then open http://127.0.0.1:8000/health
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
│   └── db/
│       ├── base.py          Base, UTC datetime type, status enums
│       ├── models.py        8 tables: emails, attachments, classifications, extractions,
│       │                    drafts, approvals, audit_logs, processing_errors
│       └── session.py       Engine (SQLite FK pragma on), sessions, db_ping
├── migrations/              Alembic (alembic.ini at project root)
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
