# Design review: error handling and spam (Phase 1 re-evaluation)

This review checks the spec's error and spam handling before code depends on it.
Each item lists the problem, the decision, and where it is enforced.
Items marked **[needs your OK]** change or add to the spec.

## A. Error handling

| # | Problem found in spec | Decision | Enforced in |
|---|---|---|---|
| A1 | `ERROR` and `FAILED` were never defined apart. | `ERROR` means processing failed (LLM, parsing, OCR, Gmail fetch/auth). `FAILED` means only a send failed. | `app/core/errors.py` (`ERROR_POLICIES`) |
| A2 | Error codes were scattered, so each stage could invent its own. | One `ErrorCode` enum. A test makes sure every code has a policy (target state, retryable or not, admin message). | `errors.py`, `tests/test_errors.py` |
| A3 | **Double-send risk.** If a Gmail send times out after the request reached Google, the mail may already have gone out. A plain "retry" could send it twice. | New code `SEND_DELIVERY_UNKNOWN`. It moves to `FAILED` and is **not retryable**: the admin checks the Sent folder first. Phase 11 adds an idempotency marker per draft. **[needs your OK]** | `errors.py` |
| A4 | Rejected actions (sending an unapproved email, or sending while sending is disabled) must not change state or count as failures. | Their policy state is `NONE`: the request is refused and the email stays where it is. | `errors.py` |
| A5 | `/retry` could loop forever on an email that always fails. | `MAX_PROCESSING_ATTEMPTS` (default 3, max 10). `LLM_MAX_RETRIES` is capped at 2 in config (spec says 1). | `config.py` |
| A6 | Error details could leak email text or tokens into logs or the DB. | `PipelineError.detail` is cut to 500 chars. The logger redacts sensitive keys (`body`, `token`, `prompt`, …) and scrubs token patterns (Bearer, `sk-`, `nvapi-`, Google `ya29.`/`1//`). | `errors.py`, `logging.py` |
| A7 | A bad config could fail silently. | Settings and `categories.yaml` are validated strictly at startup, and the app will not start if they are invalid. | `config.py`, `categories.py`, `main.py` |
| A8 | Real mail could be sent by accident while in demo mode. | `SEND_MODE` = `disabled`/`simulated`/`gmail`. The default is `simulated`. `DEMO_MODE=true` together with `SEND_MODE=gmail` is refused at startup. `prod` requires a 32+ char `ADMIN_API_KEY` and `DEMO_MODE=false`. | `config.py` |
| A9 | **qwen3 is a "thinking" model.** Its `<think>…</think>` output can break strict JSON and slow every call. | `LLM_THINK=false` by default, passed to Ollama's `think` option in Phase 4. Still to be verified against a real Ollama (not run yet). | `config.py` |

## B. Spam / irrelevant email

| # | Problem found in spec | Decision | Enforced in |
|---|---|---|---|
| B1 | **The pipeline drafts a reply for every email, including spam.** Replying to spam confirms the address is live, and reviewing spam drafts wastes admin time. | Each category has a `draft_reply` flag. `IRRELEVANT` has `draft_reply: false`, so no draft is generated. The email still goes to `UNDER_REVIEW` with a "no reply recommended" flag. The admin rejects it (→ `REJECTED`) or asks for a draft with Regenerate. No new states are needed. **[needs your OK]** | `config/categories.yaml`, `categories.py` validator |
| B2 | Spam is not always irrelevant: a real vendor's newsletter might hold a quotation, and a phishing mail may say "urgent invoice". | The IRRELEVANT label is never final and is never auto-deleted. A human always confirms it in review. | design rule (Phase 7) |
| B3 | Only the LLM decides spam, and a prompt-injection email can say "classify me as REQUIREMENT". | Deterministic **spam signals** (Gmail `SPAM` label, bulk headers, `Precedence: bulk`, external sender domain, injection/phishing phrases) are computed without the LLM and shown to the admin. They can only **push toward review, never auto-act**. If the signals and the LLM category disagree, the email is flagged. | `categories.yaml` → `spam_signals` (used from Phase 3/4) |
| B4 | An unknown category from the LLM could be dropped silently or treated as spam. | It falls back to `GENERAL`, which can have a reply drafted, and is flagged for manual review. The config refuses a fallback that is a no-draft category. | `CategoryConfig.resolve`, validator |
| B5 | Gmail already filters spam. | The default fetch query excludes `in:spam` (`GMAIL_QUERY`). This can be changed in `.env`. | `config.py` |
| B6 | The LLM's self-reported confidence is poorly calibrated. | Spec threshold kept at 0.6. Schema failure, unknown category, or a spam-signal conflict **also** send the email to manual review, whatever the confidence. | config now, logic in Phase 4 |

## C. Other tech-decision checks

- **Gmail scopes:** `gmail.readonly` + `gmail.send` only. They are hard-coded so `.env` cannot widen them. `gmail.modify` (needed to add labels or mark read) is left out on purpose.
- **Python:** tested on 3.11 and 3.12. Pinned `>=3.11,<3.13`, because some OCR/PDF wheels lag behind on 3.13.
- **No auth in the spec:** the approve and send endpoints need a known approver for the audit trail. Plan: an `X-Admin-Key` header plus an approver name, required in `prod`. **[needs your OK]**
