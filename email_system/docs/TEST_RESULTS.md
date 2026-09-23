# Test results

Labels:
- **EXECUTED**: run by me in the build environment; output is in this repo or quoted.
- **NOT RUN**: not executed; why is stated.

Build environment: Linux container, 4 CPU cores, **no GPU**, 15 GB RAM, Python 3.12.3 and
3.11.15, Ollama 0.34.3, Tesseract 5.3.4, PostgreSQL 16, Chromium (Playwright).
**Nothing was run on macOS.**

## 1. Automated test suite (pytest): EXECUTED

| Run | Result |
|---|---|
| Python 3.12, `CHROMIUM_EXECUTABLE` set | **429 passed, 1 skipped** (skip = optional PostgreSQL test) |
| Python 3.11 | **426 passed, 4 skipped** (3 browser tests: no browser configured for that run; 1 PostgreSQL) |
| `TEST_POSTGRES_URL` → real PostgreSQL 16 | PostgreSQL test **passed**. All migrations upgrade / `alembic check` / downgrade cleanly. |

The LLM and Gmail are **mocked** in pytest: a scripted `FakeProvider` and a fake Gmail
service. Tesseract and PDF parsing are **real**.

### The 15 tests required by the spec

Every failure test calls `assert_nothing_sent(db)`. It checks the DB status, `sent_at`,
the provider id, that no `EMAIL_SENT` audit row exists, **and** that the simulated outbox
is empty. It is used 40 times.

| # | Spec test | Test functions (file) |
|---|---|---|
| 1 | Normal requirement | `test_normal_requirement_classified` (test_classify), `test_normal_requirement_extracted` (test_extract), `test_process_requirement_end_to_end_to_review` (test_draft) |
| 2 | Vendor quotation | `test_vendor_quotation_from_body` (test_extract), `test_spec5_pdf_quotation_end_to_end` (test_attachments) |
| 3 | Irrelevant email | `test_irrelevant_email` (test_classify), `test_spam_gets_no_draft_and_goes_to_review` (test_draft) |
| 4 | Missing specs | `test_missing_specs_listed_even_if_llm_forgets` (test_extract) |
| 5 | PDF quotation | `test_spec5_pdf_quotation_end_to_end`, `test_text_layer_pdf` (test_attachments) |
| 6 | Scanned quotation (OCR) | `test_spec6_scanned_quotation_via_ocr_end_to_end`, `test_scanned_pdf_is_ocrd` (test_attachments) |
| 7 | Prompt injection | `test_prompt_injection_is_wrapped_as_untrusted_data`, `test_fooled_llm_still_flagged_by_rules` (test_classify), `test_red_flagged_email_gets_no_automatic_draft_even_if_misclassified` (test_draft), `test_injection_inside_pdf_is_flagged` (test_attachments), `test_email_html_and_scripts_are_rendered_as_text` (browser) |
| 8 | Malformed LLM JSON | `test_malformed_json_retried_once_with_stricter_prompt`, `test_malformed_json_twice_goes_to_error_and_nothing_sent` (test_classify) |
| 9 | Gmail auth failure | `test_spec9_gmail_auth_failure_on_list`, `test_send_mode_gmail_without_token_sends_nothing` (test_gmail) |
| 10 | Duplicate email | `test_duplicate_message_id_ignored`, `test_duplicate_race_handled_via_unique_constraint` (test_ingestion), `test_sync_ingests_and_dedupes` (test_gmail) |
| 11 | Admin edits draft | `test_admin_edit_creates_version_and_diff_in_audit_not_logs`, `test_edit_after_approval_withdraws_approval` (test_workflow) |
| 12 | Admin rejects | `test_reject`, `test_reject_after_approval_and_reopen` (test_workflow), `test_reject_and_reopen` (test_api_emails) |
| 13 | Approve + send | `test_approve_then_send_simulated` (test_workflow), `test_edit_approve_send_via_api` (test_api_emails), `test_full_review_flow_in_browser` (browser) |
| 14 | Send failure | `test_send_rejected_by_provider_goes_to_failed_and_can_retry`, `test_unknown_send_outcome_blocks_any_resend` (test_workflow), `test_gmail_send_errors_map_to_safe_outcomes` (test_gmail) |
| 15 | LLM unavailable | `test_llm_unavailable_goes_to_error_without_retry`, `test_llm_unavailable_via_real_ollama_client` (test_classify), `test_llm_unavailable_returns_503_and_email_in_error` (test_api_emails) |

Extra safety tests:
- `test_only_send_email_can_send`: AST scan for a single send path.
- `test_no_path_to_sent_skips_under_review`: graph check.
- `test_send_refused_if_draft_changed_after_approval_even_directly_in_db`.
- `test_invented_values_are_removed_and_flagged`.
- Source must be ASCII-only.

## 2. Real LLM evaluation (qwen3:4b, CPU): EXECUTED

17 fictional emails: 10 demo emails, which were used for tuning, plus 7 held-out emails
that were not. Raw logs are in `docs/eval/`.

| Task | Result |
|---|---|
| Classification (`classify-v3`) | 15/17 correct. Both misses were held-out attacks, and both were flagged for review. qwen3:8b: 15/17, 2× slower. |
| Suspicious emails flagged for review | 4/4 (both models) |
| Legitimate emails wrongly flagged | 0 |
| Extraction with PDF text + OCR (`extract-v1`) | **42/42 field checks**. 2 values removed as not found in the email; both were real inventions. |
| Draft checks (`draft-v1`) | 59/60. The only miss: the scanned-quote draft didn't ask for taxes. |
| Emails sent during evals | 0 |

Self-reported confidence was 0.95 on almost every answer, including wrong ones. It is not
used as a safety signal.

## 3. End-to-end demo (real server + real qwen3:4b + simulated sender): EXECUTED

`scripts/e2e_demo.py`, full transcript in `docs/eval/2026-09-23_e2e_demo_run.txt`:
1. **Processing:** all 10 demo emails processed through the HTTP API and ended in
   `UNDER_REVIEW`. Spam and suspicious emails got no draft. PDF read by text layer, scan
   read by OCR. 16–105 s per email on CPU.
2. **Send before approval:** refused with **409** `SEND_NOT_APPROVED`.
3. **Approve + send:** two replies approved and sent. Exactly **2 `.eml` files** in the
   outbox, threaded with `In-Reply-To`.
4. **Reject:** the spam was rejected. Final state: 7 `UNDER_REVIEW`, 2 `SENT`,
   1 `REJECTED`.
5. **Audit trail** of a sent email: received → classified → extracted → draft → review →
   approved → send started → sent.
6. **Observed variation:** this run classified the phishing email as `GENERAL` (earlier
   runs: `IRRELEVANT`). It still got **no draft** (rule signals + red flags) and showed 4
   review reasons.
7. **Draft quality:** one draft asked the requester for vendor quotes, which a human
   editor should remove.

## 4. Browser tests (Playwright + Chromium): EXECUTED

4 passed:
- full edit → approve → send flow;
- cancelled send dialog sends nothing;
- XSS payloads rendered as text;
- security headers.

No console errors. Screenshots are in `docs/screenshots/`.

## 5. Security scanners: EXECUTED

- bandit: no issues;
- pip-audit: no known vulnerabilities;
- secret-pattern scan: clean.

See `SECURITY_REVIEW.md`.

## NOT RUN

| Item | Why |
|---|---|
| Real Gmail account (OAuth consent, fetch, send) | No Gmail account in the build environment. Gmail code is tested against a mocked API only. |
| macOS / Apple Silicon | Built on Linux. Mac install steps are written but not executed; Mac timings not measured. |
| React/Vite frontend | Not built: plain HTML/JS was chosen (allowed by the spec). |
| Load / concurrency testing beyond the optimistic-lock tests | Out of scope for v1 |
| Real (non-fictional) institutional emails | None available. The 17-email test set is small; accuracy numbers are indicative only. |
