"""Security invariants checked on the source code itself (Phase 12)."""

import ast
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import get_settings

APP = Path(__file__).resolve().parents[1] / "app"


def _send_calls():
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "send"
                    ):
                        yield path.relative_to(APP).as_posix(), node.name


def test_only_send_email_can_send():
    """Rule 1: the only code that calls a sender is workflow.actions.send_email (the
    guarded path). Senders themselves call the provider inside app/sending/."""
    calls = {c for c in _send_calls() if not c[0].startswith("sending/")}
    assert calls == {("workflow/actions.py", "send_email")}, calls


def test_no_html_sinks_in_dashboard():
    js = (APP / "static" / "app.js").read_text(encoding="utf-8")
    for sink in (
        "innerHTML =",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
    ):
        assert sink not in js, sink


def test_prod_hides_docs_and_config(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("ADMIN_API_KEY", "k" * 40)
    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app()) as c:
        assert c.get("/docs").status_code == 404
        assert c.get("/openapi.json").status_code == 404
        assert c.get("/health").json().keys() == {"status", "database_ok"}
        assert c.get("/emails").status_code == 401
        assert c.post("/demo/emails").status_code in (401, 404)
