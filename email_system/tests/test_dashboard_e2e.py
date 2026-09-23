"""Browser tests of the dashboard (real Chromium via Playwright, fake LLM, live server).

Skipped automatically if Playwright/Chromium is not available. If your Playwright version
has no matching browser download, point CHROMIUM_EXECUTABLE at any Chromium/Chrome binary.
Screenshots are written to docs/screenshots/ for the documentation.
"""

import os
import socket
import threading
import time
from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api")

from app.api.deps import get_db, get_llm_provider, get_sender_factory  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.sending.simulated import SimulatedSender  # noqa: E402
from tests.fakes import FakeProvider, classification  # noqa: E402

SHOTS = Path(__file__).resolve().parents[1] / "docs" / "screenshots"
SIG = "IT/Admin Office"
EXTRACT = {
    "department": "CSE department",
    "request_type": "procurement",
    "items": [{"name": "Desktop PCs", "quantity": 20}, {"name": "UPS 1 kVA", "quantity": 20}],
    "budget": "INR 12,00,000",
    "deadline": "15 October 2026",
    "technical_specifications": "Intel Core i5 (13th gen), 16 GB RAM, 512 GB NVMe SSD",
    "missing_information": [],
}
DRAFT = {
    "subject": "Re: Requirement: 20 desktop PCs for new AI lab (CSE)",
    "body": (
        "Dear Dr. Kulkarni,\n\nThank you for your request for 20 Desktop PCs and 20 UPS "
        "1 kVA units for the new AI lab, with an approved budget of INR 12,00,000 and a "
        f"deadline of 15 October 2026. We will obtain vendor quotations and update you."
        f"\n\n{SIG}"
    ),
    "missing_information": [],
    "reason_for_reply": "Acknowledge the requirement.",
    "requires_human_review": True,
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(session_factory):
    import uvicorn

    from app.main import create_app

    app = create_app()
    llm = FakeProvider([])
    outbox = SimulatedSender(get_settings().outbox_dir)

    def _db():
        with session_factory() as s:
            yield s

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_llm_provider] = lambda: llm
    app.dependency_overrides[get_sender_factory] = lambda: lambda: outbox
    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield {"url": f"http://127.0.0.1:{port}", "llm": llm, "outbox": outbox}
    srv.should_exit = True
    t.join(timeout=5)


@pytest.fixture
def page(server):
    with pw.sync_playwright() as p:
        try:
            exe = os.environ.get("CHROMIUM_EXECUTABLE")
            browser = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium not available: {exc}")
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        pg = ctx.new_page()
        pg.console_errors = []
        pg.on("console", lambda m: m.type == "error" and pg.console_errors.append(m.text))
        pg.on("pageerror", lambda e: pg.console_errors.append(str(e)))
        yield pg
        browser.close()


def login(page, url, name="Asha"):
    page.goto(url + "/")
    page.fill("#admin-name", name)
    page.click("#who button")


def test_full_review_flow_in_browser(server, page):
    url = server["url"]
    login(page, url)
    page.get_by_role("button", name="Load demo emails").click()
    page.wait_for_selector("#queue tbody tr.row >> nth=9")
    assert page.locator("#queue tbody tr.row").count() == 10
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / "01_queue.png"), full_page=True)

    page.locator("#queue tr.row", has_text="20 desktop PCs").click()
    page.wait_for_selector("#original")
    server["llm"].responses[:] = [classification("REQUIREMENT", 0.95), EXTRACT, DRAFT]
    page.get_by_role("button", name="Process with AI").click()
    page.wait_for_selector("#ai-banner")
    assert page.inner_text("#ai-banner") == "AI GENERATED \u2014 HUMAN REVIEW REQUIRED"
    page.screenshot(path=str(SHOTS / "02_detail_review.png"), full_page=True)

    # Approving unsaved edits is blocked (approval covers the saved text only).
    page.fill("#draft-body", DRAFT["body"] + "\nP.S. edited")
    page.click("#btn-approve")
    page.wait_for_selector("#toast.error")
    assert "Save your edits first" in page.inner_text("#toast")
    page.click("#btn-save")
    # (string-eval waits are blocked by our CSP, which is the point; use selectors)
    page.wait_for_selector("#draft p.muted:has-text('Version 2')")
    page.click("#btn-approve")
    page.wait_for_selector("#btn-send")
    page.once("dialog", lambda d: d.accept())
    page.click("#btn-send")
    page.wait_for_selector("text=Sent ")
    page.screenshot(path=str(SHOTS / "03_sent_with_audit.png"), full_page=True)
    assert "SENT" in page.inner_text("#original")
    audit = page.inner_text("#audit")
    for event in ("ADMIN_APPROVED", "EMAIL_SENT", "DRAFT_EDITED"):
        assert event in audit
    assert len(server["outbox"].sent_files()) == 1
    assert page.console_errors == []


def test_cancelling_send_dialog_sends_nothing(server, page):
    url = server["url"]
    login(page, url)
    page.get_by_role("button", name="Load demo emails").click()
    page.wait_for_selector("#queue tbody tr.row")
    page.locator("#queue tr.row", has_text="20 desktop PCs").click()
    server["llm"].responses[:] = [classification(), EXTRACT, DRAFT]
    page.get_by_role("button", name="Process with AI").click()
    page.wait_for_selector("#btn-approve")
    page.click("#btn-approve")
    page.wait_for_selector("#btn-send")
    page.once("dialog", lambda d: d.dismiss())
    page.click("#btn-send")
    time.sleep(0.5)
    assert server["outbox"].sent_files() == []
    assert "APPROVED" in page.inner_text("#original")


def test_email_html_and_scripts_are_rendered_as_text(server, page):
    """XSS attempt: markup in subject/sender/body must show as text and never execute."""
    import httpx

    url = server["url"]
    payload = {
        "custom": {
            "sender": "Evil <evil@x.example>",
            "subject": '<img src=x onerror="window.__xss=1">Urgent',
            "body": '<script>window.__xss=2</script>\n<b onmouseover="window.__xss=3">hi</b>',
        }
    }
    r = httpx.post(url + "/demo/emails", json=payload)
    eid = r.json()[0]["email_id"]
    login(page, url)
    page.wait_for_selector("#queue tbody tr.row")
    assert '<img src=x onerror="window.__xss=1">Urgent' in page.inner_text("#queue")
    page.goto(f"{url}/#/email/{eid}")
    page.wait_for_selector("#email-body")
    assert "<script>window.__xss=2</script>" in page.inner_text("#email-body")
    page.hover("#email-body")
    assert page.evaluate("window.__xss") is None
    assert page.locator("img").count() == 0 and page.locator("#view script").count() == 0
    page.screenshot(path=str(SHOTS / "04_xss_rendered_as_text.png"), full_page=True)


def test_security_headers_on_dashboard(server):
    import httpx

    r = httpx.get(server["url"] + "/")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert httpx.get(server["url"] + "/static/app.js").status_code == 200
