# Design review: error handling and spam (Phase 1 re-evaluation)

This review checks the spec's error and spam handling before code depends on it.
Each item lists the problem, the decision, and where it is enforced.
Items marked **[accepted]** changed or added to the spec. The user approved them on 2026-09-23.

## A. Error handling

| # | Problem found in spec | Decision | Enforced in |
|---|---|---|---|
| A1 | `ERROR` and `FAILED` were never defined apart. | `ERROR` means processing failed (LLM, parsing, OCR, Gmail fetch/auth). `FAILED` means only a send failed. | `app/core/errors.py` (`ERROR_POLICIES`) |
| A2 | Error codes were scattered, so each stage could invent its own. | One `ErrorCode` enum. A test makes sure every code has a policy (target state, retryable or not, admin message). | `errors.py`, `tests/test_errors.py` |
| A3 | **Double-send risk.** If a Gmail send times out after the request reached Google, the mail may already have gone out. A plain "retry" could send it twice. | New code `SEND_DELIVERY_UNKNOWN`. It moves to `FAILED` and is **not retryable**: the admin checks the Sent folder first. Phase 11 adds an idempotency marker per draft. **[accepted]** | `errors.py` |
| A4 | Rejected actions (sending an unapproved email, or sending while sending is disabled) must not change state or count as failures. | Their policy state is `NONE`: the request is refused and the email stays where it is. | `errors.py` |
| A5 | `/retry` could loop forever on an email that always fails. | `MAX_PROCESSING_ATTEMPTS` (default 3, max 10). `LLM_MAX_RETRIES` is capped at 2 in config (spec says 1). | `config.py` |
| A6 | Error details could leak email text or tokens into logs or the DB. | `PipelineError.detail` is cut to 500 chars. The logger redacts sensitive keys (`body`, `token`, `prompt`, …) and scrubs token patterns (Bearer, `sk-`, `nvapi-`, Google `ya29.`/`1//`). | `errors.py`, `logging.py` |
| A7 | A bad config could fail silently. | Settings and `categories.yaml` are validated strictly at startup, and the app will not start if they are invalid. | `config.py`, `categories.py`, `main.py` |
| A8 | Real mail could be sent by accident while in demo mode. | `SEND_MODE` = `disabled`/`simulated`/`gmail`. The default is `simulated`. `DEMO_MODE=true` together with `SEND_MODE=gmail` is refused at startup. `prod` requires a 32+ char `ADMIN_API_KEY` and `DEMO_MODE=false`. | `config.py` |
| A9 | **qwen3 is a "thinking" model.** Its `<think>…</think>` output can break strict JSON and slow every call. | `LLM_THINK=false` by default, passed to Ollama's `think` option in Phase 4. Still to be verified against a real Ollama (not run yet). | `config.py` |

## B. Spam / irrelevant email

| # | Problem found in spec | Decision | Enforced in |
|---|---|---|---|
| B1 | **The pipeline drafts a reply for every email, including spam.** Replying to spam confirms the address is live, and reviewing spam drafts wastes admin time. | Each category has a `draft_reply` flag. `IRRELEVANT` has `draft_reply: false`, so no draft is generated. The email still goes to `UNDER_REVIEW` with a "no reply recommended" flag. The admin rejects it (→ `REJECTED`) or asks for a draft with Regenerate. No new states are needed. **[accepted]** | `config/categories.yaml`, `categories.py` validator |
| B2 | Spam is not always irrelevant: a real vendor's newsletter might hold a quotation, and a phishing mail may say "urgent invoice". | The IRRELEVANT label is never final and is never auto-deleted. A human always confirms it in review. | design rule (Phase 7) |
| B3 | Only the LLM decides spam, and a prompt-injection email can say "classify me as REQUIREMENT". | Deterministic **spam signals** (Gmail `SPAM` label, bulk headers, `Precedence: bulk`, external sender domain, injection/phishing phrases) are computed without the LLM and shown to the admin. They can only **push toward review, never auto-act**. If the signals and the LLM category disagree, the email is flagged. | `categories.yaml` → `spam_signals` (used from Phase 3/4) |
| B4 | An unknown category from the LLM could be dropped silently or treated as spam. | It falls back to `GENERAL`, which can have a reply drafted, and is flagged for manual review. The config refuses a fallback that is a no-draft category. | `CategoryConfig.resolve`, validator |
| B5 | Gmail already filters spam. | The default fetch query excludes `in:spam` (`GMAIL_QUERY`). This can be changed in `.env`. | `config.py` |
| B6 | The LLM's self-reported confidence is poorly calibrated. | Spec threshold kept at 0.6. Schema failure, unknown category, or a spam-signal conflict **also** send the email to manual review, whatever the confidence. | config now, logic in Phase 4 |

## C. Other tech-decision checks

- **Gmail scopes:** `gmail.readonly` + `gmail.send` only. They are hard-coded so `.env` cannot widen them. `gmail.modify` (needed to add labels or mark read) is left out on purpose.
- **Python:** tested on 3.11 and 3.12. Pinned `>=3.11,<3.13`, because some OCR/PDF wheels lag behind on 3.13.
- **No auth in the spec:** the approve and send endpoints need a known approver for the audit trail. Plan: an `X-Admin-Key` header plus an approver name, required in `prod`. **[accepted]**

## D. Phase 2 (database) follow-ups

- **Double approve/send race.** Two admins, or a double-click, could both move the same email. `emails.version` is an optimistic lock: the second write fails with `StaleDataError` instead of silently winning. The API will turn that failure into HTTP 409 (Phase 8).
- **Audit evidence can't be deleted.** Deleting an email that has audit logs, approvals or errors is refused by the DB (RESTRICT). Pipeline outputs (drafts, attachments, …) cascade.
- **"Human review required" is a DB rule.** A draft row with `requires_human_review=false` cannot be stored.
- **What was approved.** An `APPROVED` decision must reference the exact `draft_id`, so the audit trail shows the exact text that was approved. Admin edits create a new draft version with a parent link, and the diff is computed in Phase 7.
- **Portability.** Enums are stored as VARCHAR + CHECK. The partial index has an explicit filter for each dialect. The same migration was run on SQLite **and a real PostgreSQL 16**.

## E. Phase 3 (ingestion) decisions

- **Hidden HTML text is kept out of the body, and flagged.** Text hidden with `display:none`, `visibility:hidden`, `font-size:0`, `opacity:0` or the `hidden` attribute is a common way to smuggle instructions to an AI. The admin can't see it, so the LLM doesn't get it either. It still counts as a review reason, and phrase checks scan it.
- **Invisible / bidi characters** (zero-width, soft hyphen, RTL override, BOM) are removed and counted before phrase checks, so `Ig<ZWSP>nore previous instructions` is still caught. NFKC folds full-width look-alike letters.
- **Phrase lists are a weak signal.** A reworded injection gets past them. The real defences come later: every email goes into the prompt wrapped as delimited untrusted data (Phase 4), LLM outputs are validated against strict schemas, and nothing is sent without human approval. Signals only add review flags.
- **Which signals force review:** Gmail SPAM label, suspicious phrase, hidden HTML text, dangerous attachment extension, attachment content that doesn't match its type, oversized attachment, unparseable sender. Bulk headers, `external_sender` and `reply_to_mismatch` are shown as hints only, because legitimate mailing systems trigger them too.
- **Attachments** are stored under their SHA-256 hash (`data/attachments/ab/<sha>.bin`). The sender-controlled filename is only a display label, so path traversal isn't possible. Writes are atomic. Files over `MAX_ATTACHMENT_BYTES` are recorded (name, size, hash) but not stored.
- **Duplicates:** checked by `message_id` before insert. The DB unique constraint catches concurrent inserts. Each duplicate adds a `DUPLICATE_IGNORED` audit row to the original email. Nothing else changes.
- **Ingestion never changes state beyond `NEW`**, so it can't approve or send anything.
- **Source hygiene:** a test fails if any `.py` file contains non-ASCII characters. This came up in this phase: the editor turned `\u200b` escapes into literal invisible characters, and the test stops that happening again.
- **Not done (known limitations):**
  - Quoted reply chains (`> ...`, "On ... wrote:") are kept in the body. Phase 4 will decide whether to trim them before prompting.
  - Sample emails with an inline image and no body are not covered.

## F. Phase 4 (LLM + classification) decisions

- **One transition table** (`app/workflow/states.py`) for every status change. Tests check
  that `APPROVED` can only follow `UNDER_REVIEW`, that `SENT` can only follow
  `APPROVED`/`FAILED`, and that no path reaches `SENT` without passing `UNDER_REVIEW`.
- **Prompt-injection defence has four layers:**
  1. The email is wrapped in markers that contain a random nonce, and any `<<<`/`>>>`
     inside the email is broken up.
  2. The system prompt says the email is untrusted data.
  3. The model reports `red_flags` in a separate field.
  4. The rule-based signals from ingestion.

  None of these alone was enough on real models (see `MODEL_CHOICE.md`). Together they
  flagged every suspicious email in the test set. The category the model picks is never a
  safety control.
- **Structured output:** the JSON schema is written out inline (no `$ref`) and sent to the
  model to constrain its output, then validated by Pydantic with `extra="forbid"`. If
  validation fails, the call is retried once with a stricter prompt that includes the
  validation error. After that the email goes to `ERROR`. Connection errors and timeouts
  are not retried with a stricter prompt; the email goes straight to `ERROR` and the admin
  retries it.
- **Cap on attempts:** `MAX_PROCESSING_ATTEMPTS` is checked *before* the LLM is called.
- **Rule-signal details are kept out of the trusted part of the prompt:** only bare codes
  go there, because the details can include sender-controlled text such as filenames.
- **No LLM text in logs:** neither the prompt, the reason nor the response is logged. Audit
  rows store the category, confidence, flags, model and prompt version.
- **Seen in a real run:** Ollama's model load can be killed when memory runs out. This
  shows up as `LLM_UNAVAILABLE` → `ERROR`, and the email can be retried.

## G. Phase 5 (extraction) decisions

- **Rule 2 is enforced in code, not only in the prompt.** The fact-check module
  (`grounding.py`) removes values that can't be traced to the sender's text. It is
  deliberately conservative: a correct value the model reworded is dropped and shows up as
  missing. That is safer than a confident invented value.
- **Bug found and fixed during development:** the first version checked values against the
  whole prompt. A quantity of `10` was then "confirmed" by the random delimiter or the
  timestamp. The check now uses only the sender's text, and the number match ignores
  digits inside IDs, dates and amounts (`INV-7781`, `20-10-2026`, `12,00,000`).
- **Seen in a real run:** qwen3:4b changed `20-09-2026` to `2026-09-20` even though the
  prompt says to copy exactly. For date fields, an ISO date is kept only if the email has
  the same date in day-first or month-name form, and it is stored in the email's spelling.
  Month-first numeric dates are never assumed.
- **Amounts are kept as strings.** The model never does arithmetic, and nothing is summed
  or converted. Example: the toner quote gives a unit price but no total, so `total`
  stays null and "total" is listed as missing.
- **The status stays `CLASSIFIED`.** The spec has no EXTRACTED state; an `EXTRACTED` audit
  event is written. Categories with `extraction_schema: none` skip the LLM call.
- **Attachments not read yet** (text layer and OCR come in Phase 10) are marked in the
  prompt as "content not available", listed in `missing_information`, and flagged for
  review.

## H. Phase 6 (drafting) decisions

- **Guidance for each category**, in the prompt:
  - quotations: acknowledge, never accept or order;
  - invoices: acknowledge, never confirm payment or a change of bank details;
  - technical queries: acknowledge, never claim a fix.

  The drafting prompt forbids stating that anything is approved, paid, ordered or scheduled.
- **The extracted data counts as untrusted too.** It comes from the email, so it sits in
  its own delimited block, not in the trusted part of the prompt.
- **Admin regenerate instructions are trusted.** They come from the authenticated admin.
  They're capped at 1000 characters and sit outside the untrusted blocks.
- **Draft warnings** are computed in code and flag, without editing, anything in the draft
  that the email doesn't support: numbers, links, addresses, commitment phrases. Missing
  information from extraction is always carried into the draft's list.
- **State flow:** `CLASSIFIED -> DRAFT_GENERATED -> UNDER_REVIEW` in one transaction, with
  `DRAFT_GENERATED` and `MOVED_TO_REVIEW` audit events. With no draft (spam):
  `CLASSIFIED -> UNDER_REVIEW` plus a `NO_DRAFT_NEEDED` event.
- **One current draft.** Regenerating clears the old draft's `is_current` flag before the
  new one is inserted, because the database allows only one current draft per email.

- **Seen in a real run (Phase 6 eval):** the two held-out attack emails were misclassified
  (Phase 4) and at first got friendly drafts, one repeating the attacker's "pre-approved"
  claim. Fix: **any red flag (from the LLM or the rule-based checks) blocks automatic
  drafting**. The email goes to review, and an admin can still request a draft. After the
  fix: 59/60 draft checks and no drafts for any suspicious email. In the measured runs no
  legitimate email had red flags, so the rule cost nothing there.

## I. Phase 7 (approval + sending) decisions

- **The approval covers exact bytes.** `approvals.content_sha256` is stored at approval
  time (a DB CHECK makes it required) and re-checked at send time. A test changes the
  draft directly in SQL after approval, and sending is refused.
- **Double-send protection:** `emails.send_started_at` is committed *before* the provider
  is called.
  - Success or a confirmed rejection (`SendRejected`) clears it.
  - Any other exception (timeout, crash) leaves it set: `FAILED` with
    `SEND_DELIVERY_UNKNOWN`, not retryable, and edits are blocked too.
  - A crash after the provider call but before the DB update is covered the same way.
- **Stale-view protection:** approve and edit take the draft id the admin saw. If someone
  else changed the draft in the meantime, the action is refused (`STALE_DRAFT`).
- **Warnings need an explicit acknowledgement** before approval
  (`WARNINGS_NOT_ACKNOWLEDGED`).
- **Added transitions:** `APPROVED -> REJECTED` and `FAILED -> REJECTED` (an admin changes
  their mind before sending). Tests still check that `SENT` is reachable only through
  `APPROVED`.
- **Regenerate is an explicit admin request.** It may draft for spam or red-flagged
  emails. If the email was approved, the approval is withdrawn first.
- **Retry:** `ERROR -> NEW`, refused for non-retryable errors (e.g. oversized
  attachment). The attempt limit from Phase 4 still applies.
- **Not done yet:** there is no "confirm not delivered" action to clear an unknown send
  outcome, so for now it needs a DB fix. This belongs with Gmail (Phase 11), where the
  Sent folder can be checked automatically.

## J. Phase 8 (API) decisions

- **Two headers:** `X-Admin-Key` answers "may this client use the API?".
  `X-Admin-Name` answers "which person is acting?" and is written to the audit trail.
  There are no individual accounts in v1, so the name is self-declared by whoever holds
  the key. Individual logins are a documented limitation.
- **The LLM and the sender are injected as dependencies.** The sender is created lazily,
  only when a send really happens. Found while reviewing my own code: with an eager
  sender, `POST /retry` on an LLM error failed with 403 whenever `SEND_MODE=disabled`.
- **`POST /process` only accepts NEW/CLASSIFIED emails.** Anything else is a 409, never a
  silent no-op.
- **`allowed_actions`** in the detail response is only a hint for the dashboard. Every
  action is checked again on the server.
- **Error bodies** contain the code, a fixed message and a short detail. They never
  include email text or stack traces (tested).
- **Processing is synchronous.** On CPU, one email takes 1-2 minutes. A background job
  queue is left for later; single-admin use is fine for v1.
