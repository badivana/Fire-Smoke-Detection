# Security review (Phase 12)

Scope: everything under `email_system/`. Method: automated scanners, invariants checked by
tests, and a manual review against the spec's five non-negotiable rules and the OWASP
items relevant here.

## Automated checks (executed)

| Check | Result |
|---|---|
| `bandit -r app scripts` | **No issues**. The Tesseract `subprocess` call has a justified `nosec`: fixed argv, no shell, data via stdin, timeout. |
| `pip-audit -r requirements.txt` | **No known vulnerabilities** |
| Secret-pattern grep over tracked files (AWS, Google `ya29.`, `sk-`, `nvapi-`, private keys) | **None** |
| Tracked `.env`, `token.json`, `credentials.json`, `*.db`, `secrets/` | **None** |
| `ruff` | Clean |

## Invariants enforced by tests

- **Single send path:** an AST scan finds that the only call to a sender's `.send(` is in
  `workflow.actions.send_email` (`test_security_invariants.py`).
- **State graph:** `SENT` is unreachable without `UNDER_REVIEW` → `APPROVED`
  (`test_states.py`).
- **Every failure path proves that nothing was sent:** DB status, `sent_at`, provider id,
  no `EMAIL_SENT` audit row, and an empty simulated outbox (`assert_nothing_sent`, used in
  30+ tests).
- **Approved text only:** changing the draft in SQL after approval blocks sending.
- **Dashboard:** no HTML sinks in `app.js`. A browser XSS test (script/onerror/onmouseover
  in a real email) confirms nothing runs. Strict CSP headers are present.
- **Production hardening:** `/docs` and `/openapi.json` are disabled, `/health` shows no
  config, and every API route needs the key.
- **Source hygiene:** all `.py` files are pure ASCII (no hidden bidi/zero-width
  characters).

## Manual review

| Area | Finding | Status |
|---|---|---|
| AuthN / AuthZ | One shared `X-Admin-Key` (constant-time compare, required in prod, 32+ chars). `X-Admin-Name` is self-declared. | Accepted limitation: no per-user accounts, so the name is only as trustworthy as the people holding the key. |
| Prompt injection | Email + attachment text are only placed inside nonce-delimited untrusted blocks. Hidden HTML is removed before the model sees it. Red flags + rule signals force review and block automatic drafting. | Mitigated. **Residual risk:** the category itself can be steered (measured). It is never used as a safety control. |
| Hallucination | Grounding removes untraceable values. Draft warnings flag numbers, links and commitments. | Mitigated; conservative by design. |
| XSS | textContent-only rendering and a CSP without `unsafe-inline`/`unsafe-eval`. | Mitigated, tested. |
| CSRF | The API uses custom headers (`X-Admin-Name`, `X-Admin-Key`) and JSON; no cookies are used for auth. A cross-site form can't set those headers. | Not applicable in the current design. |
| SQL injection | SQLAlchemy only, parameterised. | OK |
| Path traversal | Attachments are stored by SHA-256. Filenames are sanitised. Reading checks that the path stays inside `ATTACHMENTS_DIR`. | Mitigated, tested. |
| Malicious files | Dispatch by content (magic bytes). Page/pixel/time limits. Encrypted PDFs are not opened. Dangerous extensions and type mismatches are flagged. Attachments are never executed or rendered in the browser. | Mitigated. PyMuPDF parses untrusted PDFs in-process; keep it updated. |
| DoS | Body 200k chars stored and 12k sent to the LLM. Attachment size cap. OCR limits. Gmail raw message cap. Processing attempts capped. | Partial: no request rate limiting (run on localhost or behind a proxy). |
| Secrets | `.env` (git-ignored), `SecretStr`, Gmail token `0600`, fixed least-privilege scopes, redacting logger, no secrets in error bodies (tested). | OK |
| Logging | Bodies, prompts, diffs and tokens are redacted from logs. Diffs are kept only in the audit DB. | OK |
| Double send | `send_started_at` marker, `SEND_DELIVERY_UNKNOWN` never auto-retried, `num_retries=0` on Gmail send. | Mitigated, tested. |
| Concurrency | Optimistic lock on `emails.version`, stale-draft checks. Returns 409 on conflict. | OK |
| Data at rest | SQLite file and attachments are not encrypted by the app. | Accepted: rely on disk encryption (FileVault on the Mac). |

## Open items (not done in v1)

1. Per-admin accounts (SSO/OIDC) and role separation (approver vs. sender).
2. A "confirm not delivered" action backed by a Gmail Sent-folder lookup, to unlock
   `SEND_DELIVERY_UNKNOWN`.
3. Rate limiting and request-size limits at a reverse proxy if exposed beyond localhost.
4. Background job queue for processing (synchronous calls take 1-2 min on CPU).
5. First run against a real Gmail account (only mocked so far).
