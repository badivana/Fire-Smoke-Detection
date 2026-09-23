# Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `LLM_UNAVAILABLE` (HTTP 503) when processing | Ollama not running, or the model isn't pulled | `brew services start ollama` (or `ollama serve`), then `ollama pull qwen3:4b`. Check with `curl localhost:11434/api/tags`. The email is in `ERROR`; press **Retry**. |
| `LLM_UNAVAILABLE` right after switching to `qwen3:8b` | The model load was killed for lack of memory (seen in testing: `signal: killed` in Ollama's log) | Use `qwen3:4b`, close other apps, or `ollama stop <other model>` first. |
| `LLM_TIMEOUT` (HTTP 504) | CPU is slow or busy; the first call also loads the model | Raise `LLM_TIMEOUT_SECONDS` in `.env` (default 120). Don't run the eval and the test suite at the same time. |
| `LLM_SCHEMA_INVALID` (HTTP 502) | The model gave invalid JSON twice | Retry. If it keeps happening, check `LLM_THINK=false` (qwen3 "thinking" breaks JSON). |
| Scanned PDF shows "OCR not available" | Tesseract is not installed | `brew install tesseract`, then `tesseract --version`. Set `TESSERACT_CMD` if it isn't on `PATH`. Reopen the email and press Retry (or process a fresh copy). |
| Attachment "encrypted PDF not opened" | Password-protected PDF | Ask the sender for an unprotected copy. It is never guessed. |
| Extracted value missing though it is in the email | The model reworded it and the fact-check removed it (see "Removed because they were not found" on the detail page) | Expected, conservative behaviour. Edit the draft by hand. Amounts and dates in other formats are matched; free paraphrases are not. |
| Approve returns 409 `STALE_DRAFT` | The draft changed after you opened it | Reload the page, review the new version, approve again. |
| Approve returns 409 `WARNINGS_NOT_ACKNOWLEDGED` | The draft has automatic warnings | Read them, tick "I checked the warnings", approve. |
| Send returns 409 `SEND_NOT_APPROVED` "draft changed after approval" | The draft text differs from what was approved | Review and approve the current version. |
| Email locked, message "send attempt has an UNKNOWN outcome" | Timeout or crash during send | Check Gmail's **Sent** folder. v1 has no "mark as not sent" button: if it wasn't sent, an operator must clear `emails.send_started_at` in the DB (documented limitation). |
| Send returns 403 `SEND_DISABLED` | `SEND_MODE=disabled` | Intended. Use `simulated` for the demo or `gmail` for real sending. |
| App refuses to start: "DEMO_MODE=true cannot be combined with SEND_MODE=gmail" | Safety check | Set `DEMO_MODE=false` for Gmail. |
| App refuses to start: "APP_ENV=prod requires ADMIN_API_KEY" | Safety check | Generate a key: `python -c "import secrets;print(secrets.token_urlsafe(32))"`. |
| `GMAIL_AUTH_FAILED` | Token missing, expired or revoked, or wrong scopes | `python -m app.gmail auth` (see `GMAIL_SETUP.md`). |
| `sqlite3.OperationalError: no such table` | Migrations not run | `alembic upgrade head`. |
| 400 "X-Admin-Name header is required" | Name missing | Enter your name in the dashboard's top bar (API: send the header). |
| 401 on every request | `ADMIN_API_KEY` is set but not sent | Enter the key in the dashboard, or send `X-Admin-Key`. |
| Browser tests skipped | No Chromium matching the Playwright version | `python -m playwright install chromium`, or set `CHROMIUM_EXECUTABLE=/path/to/chrome`. |
| `pip install` fails on Python 3.13 | Some wheels lag behind | Use Python 3.12 (`brew install python@3.12`). |

Logs never contain email bodies or secrets. To see a pipeline decision, open the email's
**Audit trail** (dashboard or `GET /emails/{id}/audit`).
