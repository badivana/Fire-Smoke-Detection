import json
import logging

from app.core.logging import REDACTED, EventType, log_event, redact, scrub_text, setup_logging


def test_sensitive_keys_redacted():
    out = redact(
        {
            "email_id": 7,
            "body": "Dear admin...",
            "api_key": "abc",
            "nested": {"access_token": "t", "subject_len": 12},
        }
    )
    assert out["email_id"] == 7
    assert out["body"] == REDACTED
    assert out["api_key"] == REDACTED
    assert out["nested"]["access_token"] == REDACTED
    assert out["nested"]["subject_len"] == 12


def test_secret_patterns_scrubbed_from_free_text():
    s = scrub_text(
        "auth Bearer abc.def-123 key=sk-ABCDEFGHIJKLMNOP nv nvapi-XYZXYZXYZXYZ1 "
        "g ya29.a0AfH6SMB password=hunter2"
    )
    for leaked in ("abc.def-123", "sk-ABCDEFGHIJKLMNOP", "nvapi-XYZ", "ya29.a0", "hunter2"):
        assert leaked not in s


def test_long_values_truncated():
    assert scrub_text("a" * 1000).endswith("[truncated]")


def test_log_event_output_is_redacted_json(capsys):
    setup_logging("INFO", as_json=True)
    log_event(EventType.EMAIL_RECEIVED, email_id=1, body="SECRET BODY", model="qwen3:4b")
    line = capsys.readouterr().out.strip()
    data = json.loads(line)
    assert data["event"] == "EMAIL_RECEIVED"
    assert data["body"] == REDACTED
    assert "SECRET BODY" not in line
    assert data["model"] == "qwen3:4b"
    logging.getLogger("email_ai").handlers.clear()
