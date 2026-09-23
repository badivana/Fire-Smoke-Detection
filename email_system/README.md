# Institutional Email AI (human-in-the-loop)

This system reads, classifies and extracts details from institutional emails, then drafts replies.
**It never sends an email without explicit admin approval.**

Status: **Phase 9 of 13 (dashboard)**. See `docs/DESIGN_REVIEW.md` for the review of error and spam handling.

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
uvicorn app.main:app --reload     # dashboard: http://127.0.0.1:8000/   API docs: /docs
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
│   │   ├── deps.py          get_db, X-Admin-Key, X-Admin-Name, LLM/sender injection
│   │   ├── emails.py        /emails queue, detail, process, draft, approve, send, ...
│   │   ├── dashboard.py     /dashboard/stats
│   │   ├── errors.py        error code -> HTTP status (409/503/502/...)
│   │   └── demo.py          GET /demo/samples, POST /demo/emails
│   ├── demo/
│   │   ├── samples.yaml     10 fictional sample emails + expected results
│   │   ├── fixtures/        quotation PDF (text layer) + scanned quotation (image only)
│   │   └── __main__.py      python -m app.demo
│   ├── eval/__main__.py     python -m app.eval: classification accuracy on a REAL LLM
│   ├── llm/
│   │   ├── base.py          LLMProvider interface + HTTP error mapping
│   │   ├── ollama.py        /api/chat, JSON-schema `format`, think=false
│   │   ├── openai_compat.py /v1/chat/completions, response_format json_schema (NIM etc.)
│   │   ├── structured.py    parse + Pydantic-validate, 1 stricter retry, else LLM_SCHEMA_INVALID
│   │   └── factory.py       build_provider() from settings
│   ├── pipeline/
│   │   ├── prompts.py       untrusted-data wrapper (random-nonce delimiters), classify prompt
│   │   ├── schemas.py       ClassificationOutput + inline JSON schema
│   │   ├── classify.py      NEW -> CLASSIFIED / ERROR, review flags
│   │   ├── extract.py       requirement / quotation / invoice extraction
│   │   ├── grounding.py     removes any extracted value not found in the email
│   │   ├── draft.py         reply drafts (new version per regenerate) -> UNDER_REVIEW
│   │   ├── draft_checks.py  warnings: numbers/links/addresses not in email, commitments
│   │   └── process.py       classify -> extract -> draft -> UNDER_REVIEW (or ERROR)
│   │   └── failures.py      ProcessingError row + audit + ERROR state
│   ├── workflow/
│   │   ├── states.py        the ONE transition table + transition()
│   │   └── actions.py       edit / approve / reject / request edit / regenerate /
│   │                        reopen / retry / send_email (the only send path)
│   ├── sending/             EmailSender interface, SimulatedSender (.eml outbox)
│   ├── static/              dashboard: index.html, app.js, style.css (no build step)
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

## LLM

```zsh
ollama pull qwen3:4b
python -m app.eval -v                  # accuracy on the 10 demo emails (real model)
python -m app.eval --model qwen3:8b    # compare; only switch if 4B fails the test set
python -m app.eval --set holdout       # 7 held-out emails not used for prompt tuning
python -m app.eval --task extract --set all -v   # extraction field checks
python -m app.eval --task draft --set all -v     # full pipeline, prints every draft
```

Measured results and the reasoning for the default model are in `docs/MODEL_CHOICE.md`.

- Every email goes to the model **wrapped as untrusted data**, between
  `<<<EMAIL_<random>>>` markers. The random part is new on each call, so the email can't
  fake the end marker.
- Output is constrained by a JSON schema (Ollama `format`), then validated by Pydantic.
  If validation fails, the call is retried once with a stricter prompt. If it fails again,
  the email goes to `ERROR`.
- The LLM's confidence is not trusted on its own. An email is flagged for review when:
  - confidence is below 0.6;
  - the model returns an unknown category;
  - the model reports a red flag (instructions to an AI, credential request, payment
    change, pressure);
  - the rule-based spam checks disagree with the model;
  - the email was truncated.

## Extraction (no invented facts)

Categories with an `extraction_schema` (REQUIREMENT, VENDOR_QUOTATION, INVOICE) get
structured data. Amounts, IDs and dates are kept **as written** ("INR 12,00,000").

After the LLM answers, `grounding.py` checks every value against the sender's text
(subject, body, attachment text):
- A value that isn't there is **removed**, listed in `ungrounded_fields`, and the email is
  flagged for review.
- An item whose name isn't in the email is dropped.
- A date the model reformatted (`2026-09-20`) is kept only if the email contains the same
  date (`20-09-2026`), and it is stored in the email's spelling.

`missing_information` is worked out **in code** from the empty required fields, plus any
extra notes from the model. An attachment that hasn't been read yet (OCR comes in Phase
10) is listed as missing, never guessed.

## Drafts

- Every draft has `requires_human_review = true`. The model can't set it to anything else
  (the output format only accepts `true`), and the database refuses to store `false`.
- Automatic warnings are shown next to the draft. The draft text is never rewritten
  because of them. A warning is raised for:
  - numbers, links or email addresses that aren't in the email;
  - commitment wording ("has been approved", "payment has been made", …);
  - placeholders or markup.
- Automatic formatting only:
  - the subject is a one-line `Re: ...`;
  - the configured `REPLY_SIGNATURE` is added if the model left it out.
- `IRRELEVANT` emails get **no draft** and go to review with "No reply recommended". An
  admin can still ask for a draft (regenerate with `force`).
- Regenerating creates a new version. The previous one is kept for the audit trail, and
  admin instructions can steer the new draft.

## Approval and sending

`app/workflow/actions.py:send_email` is the **only** code that can deliver a reply. It
refuses unless **every** check passes:

1. `SEND_MODE` allows sending: `simulated` writes `.eml` files to `data/outbox/`;
   `gmail` comes in Phase 11; `disabled` refuses everything.
2. The email is `APPROVED`, or `FAILED` after a *confirmed* rejection by the provider.
3. No earlier send attempt has an unknown outcome.
4. The latest approval is for the **current draft**, and the SHA-256 of that draft's
   subject and body matches the hash stored at approval time.
5. The reply address is a valid email address.

Other rules:
- **Editing an approved draft** withdraws the approval (`APPROVED -> UNDER_REVIEW`).
- **Approving needs the draft id the admin actually saw.** A stale version is refused.
  Drafts with warnings need `acknowledge_warnings`.
- **A "send started" marker is saved before the sender is called.** It is cleared only on
  success or a confirmed rejection. After a timeout or crash, a second send (or edit) is
  blocked until a person checks the Sent folder, which prevents duplicate replies.
- **Every action needs a named admin** (not "system"). Admin edits store a unified diff
  in the audit row; the diff is never logged.

## API

Interactive docs: `http://127.0.0.1:8000/docs`.

- **Headers:**
  - `X-Admin-Key` is needed on every route when `ADMIN_API_KEY` is set (always in prod).
  - `X-Admin-Name` is needed on every call that changes something; it is recorded as the
    actor in the audit trail.

| Method & path | What it does |
|---|---|
| `GET /emails?status=&category=&needs_review=&q=&limit=&offset=` | queue |
| `GET /emails/{id}` | detail: email, attachments, classification, extraction, missing info, current draft + versions, approvals, `allowed_actions`, `ai_banner` |
| `GET /emails/{id}/audit` | audit trail (includes admin edit diffs) |
| `POST /emails/{id}/process` | classify → extract → draft → UNDER_REVIEW (synchronous; minutes on CPU) |
| `POST /emails/{id}/regenerate-draft` `{instructions?}` | new AI draft version (withdraws approval) |
| `PUT /emails/{id}/draft` `{subject, body, expected_draft_id?}` | save / edit draft (new version) |
| `POST /emails/{id}/approve` `{draft_id, acknowledge_warnings?, comment?}` | approve that exact draft |
| `POST /emails/{id}/reject` `{reason?}` · `/request-edit` `{comment}` · `/reopen` | decisions |
| `POST /emails/{id}/send` | send the approved draft (only path that sends) |
| `POST /emails/{id}/retry` | ERROR → reprocess; FAILED (confirmed rejection) → resend |
| `GET /dashboard/stats` | counts |
| `POST /demo/emails`, `GET /demo/samples` | demo mode only |

- **Error format:** `{"error": CODE, "message", "detail", "retryable"}`.
  - Workflow conflicts: 409. LLM down: 503. Bad LLM output: 502. Send disabled: 403.
  - Two admins changing the same email at once: 409 `CONCURRENT_UPDATE`.
- **Local only without a key:** with no `ADMIN_API_KEY` set, anyone who can reach the port
  can act. Keep uvicorn on `127.0.0.1` (the default) unless a key is set.

## Dashboard

Open `http://127.0.0.1:8000/`, enter your name (and the admin key if one is set), then
click **Load demo emails**.

- **Queue:** Sender | Subject | Category | Priority | Status | Date | Action, with filters
  for status, category and flag, a search box, and counts at the top.
- **Detail**, in this order: Original email → Attachments → Classification → Extracted
  data → Missing information → Draft (editable, with the **AI GENERATED — HUMAN REVIEW
  REQUIRED** banner and automatic warnings) → Save / Approve / Send / Regenerate /
  Request edit / Reject / Reopen / Retry → Audit trail.
- **Buttons shown** follow the API's `allowed_actions`; the server checks everything again.
- **Approve is blocked while there are unsaved edits.** Send asks for confirmation.

Screenshots from the browser tests: `docs/screenshots/`.

Security of the page:
- Every email/AI value is inserted with `textContent` (never `innerHTML`).
- A strict CSP allows no inline script or style and no third-party origins.
- `X-Frame-Options: DENY`, `nosniff`, `no-referrer`.
- The admin key is kept in `sessionStorage` only.
- An XSS browser test (a `<script>`/`onerror` payload in an email) confirms nothing runs.

Plain HTML/JS instead of React + Vite: the spec allows it. It means no Node toolchain
and no build step, and the page is small enough not to need a framework.
