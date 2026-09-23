"""Phase 11: Gmail parsing, sync, OAuth token handling and the Gmail sender (Gmail API
mocked; no real account is contacted). Spec test 9: Gmail auth failure."""

import base64
import json
import os
import stat
from email.message import EmailMessage

import pytest
from sqlalchemy import func, select

from app.core.config import GMAIL_SCOPES, Settings, get_settings
from app.core.errors import ErrorCode, PipelineError
from app.db.base import EmailSource
from app.db.models import Email
from app.demo.samples import FIXTURES_DIR
from app.gmail import auth as gauth
from app.gmail.sync import parse_raw, sync_gmail
from app.sending.base import OutgoingEmail, SendOutcomeUnknown, SendRejected
from app.sending.gmail import GmailSender
from tests.fakes import assert_nothing_sent


class HttpErr(Exception):
    """Mimics googleapiclient.errors.HttpError (has .resp.status)."""

    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.resp = type("R", (), {"status": status})()


class _Call:
    def __init__(self, fn):
        self.fn = fn

    def execute(self, **kw):
        return self.fn(**kw)


class FakeGmail:
    """Minimal stand-in for service.users().messages().{list,get,send}(...).execute()."""

    def __init__(self, messages=None, list_error=None, get_error=None, send=None):
        self.messages = messages or {}
        self.list_error, self.get_error, self._send = list_error, get_error, send
        self.sent = []
        self.list_kwargs = None

    def users(self):
        return self

    def list(self, **kw):
        self.list_kwargs = kw

        def run(**_):
            if self.list_error:
                raise self.list_error
            return {"messages": [{"id": i} for i in self.messages]}

        return _Call(run)

    def get(self, userId, id, format):  # noqa: A002
        def run(**_):
            if self.get_error:
                raise self.get_error
            return self.messages[id]

        return _Call(run)

    def send(self, userId, body):
        def run(**kw):
            assert kw.get("num_retries") == 0  # sends are never auto-retried
            if isinstance(self._send, Exception):
                raise self._send
            self.sent.append(body)
            return {"id": "gmail-sent-1"}

        return _Call(run)

    # users().messages() returns self
    def __call__(self):
        return self


FakeGmail.messages_ = None


def _svc(**kw):
    svc = FakeGmail(**kw)
    svc.users = lambda: type("U", (), {"messages": lambda self_: svc})()
    return svc


def raw_message(
    subject="Need 10 laptops",
    body="Please buy 10 laptops.",
    html=None,
    attach=None,
    msgid="<abc123@college.example>",
):
    m = EmailMessage()
    m["From"] = "HOD CSE <hod@college.example>"
    m["To"] = "it-admin@college.example"
    m["Subject"] = subject
    m["Date"] = "Mon, 21 Sep 2026 10:15:00 +0530"
    m["Message-ID"] = msgid
    m["Reply-To"] = "other@elsewhere.example"
    m.set_content(body)
    if html:
        m.add_alternative(html, subtype="html")
    if attach:
        name, data = attach
        m.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return base64.urlsafe_b64encode(bytes(m)).decode()


def gmsg(raw, thread="t1", labels=("INBOX",)):
    return {
        "raw": raw,
        "threadId": thread,
        "labelIds": list(labels),
        "internalDate": "1790000000000",
    }


# ---------------------------------------------------------------- parsing


def test_parse_raw_message_with_pdf():
    pdf = (FIXTURES_DIR / "quotation_acme.pdf").read_bytes()
    raw = base64.urlsafe_b64decode(raw_message(attach=("quote.pdf", pdf), html="<p>Hi</p>"))
    inc = parse_raw(raw, gmail_id="g1", thread_id="t1", labels=["INBOX"], internal_ms=None)
    assert inc.source == EmailSource.GMAIL and inc.message_id == "g1"
    assert inc.rfc_message_id == "<abc123@college.example>"
    assert inc.sender == "HOD CSE <hod@college.example>" and inc.to == ["it-admin@college.example"]
    assert inc.body_text.strip() == "Please buy 10 laptops." and inc.body_html
    assert inc.received_at.utcoffset().total_seconds() == 19800
    assert inc.headers["reply-to"] == "other@elsewhere.example"
    assert inc.attachments[0].filename == "quote.pdf" and inc.attachments[0].data == pdf


# ---------------------------------------------------------------- sync


def test_sync_ingests_and_dedupes(db):
    svc = _svc(messages={"g1": gmsg(raw_message()), "g2": gmsg(raw_message(subject="Other"))})
    s = get_settings()
    r = sync_gmail(db, svc, s)
    assert (r.listed, r.created, r.duplicates, r.failed) == (2, 2, 0, [])
    assert svc.list_kwargs["q"] == s.gmail_query and "-in:spam" in svc.list_kwargs["q"]
    r2 = sync_gmail(db, svc, s)  # spec test 10 for Gmail: same message ids again
    assert (r2.created, r2.duplicates) == (0, 2)
    assert db.scalar(select(func.count()).select_from(Email)) == 2
    e = db.scalars(select(Email).where(Email.message_id == "g1")).one()
    assert e.thread_id == "t1" and e.rfc_message_id == "<abc123@college.example>"
    assert "reply_to_mismatch" in e.spam_signals
    assert_nothing_sent(db)


def test_sync_spam_label_flags_review(db):
    svc = _svc(messages={"g9": gmsg(raw_message(), labels=("SPAM",))})
    sync_gmail(db, svc, get_settings())
    e = db.scalars(select(Email)).one()
    assert "gmail_label:SPAM" in e.spam_signals and e.needs_manual_review


@pytest.mark.parametrize("status", [401, 403])
def test_spec9_gmail_auth_failure_on_list(db, status):
    with pytest.raises(PipelineError) as ei:
        sync_gmail(db, _svc(messages={"g1": {}}, list_error=HttpErr(status)), get_settings())
    assert ei.value.code == ErrorCode.GMAIL_AUTH_FAILED
    assert db.scalar(select(func.count()).select_from(Email)) == 0
    assert_nothing_sent(db)


def test_gmail_outage_is_fetch_failed_not_auth(db):
    with pytest.raises(PipelineError) as ei:
        sync_gmail(db, _svc(list_error=HttpErr(503)), get_settings())
    assert ei.value.code == ErrorCode.GMAIL_FETCH_FAILED


def test_one_bad_message_does_not_stop_sync(db):
    svc = _svc(
        messages={"bad": {"raw": "!!!not-base64", "threadId": "x"}, "good": gmsg(raw_message())}
    )
    r = sync_gmail(db, svc, get_settings())
    assert r.created == 1 and r.failed == ["bad"]


# ---------------------------------------------------------------- OAuth token handling


def _token(tmp_path, scopes, expired=False):
    info = {
        "token": "ya29.fake-access",
        "refresh_token": "1//fake-refresh",
        "client_id": "c",
        "client_secret": "s",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": scopes,
    }
    # google-auth treats a missing expiry as already expired
    info["expiry"] = "2000-01-01T00:00:00Z" if expired else "2099-01-01T00:00:00Z"
    p = tmp_path / "token.json"
    p.write_text(json.dumps(info))
    return p


def test_missing_token_is_auth_failure(tmp_path):
    s = Settings(_env_file=None, gmail_token_file=tmp_path / "none.json")
    with pytest.raises(PipelineError) as ei:
        gauth.load_credentials(s)
    assert ei.value.code == ErrorCode.GMAIL_AUTH_FAILED and "app.gmail auth" in ei.value.detail


def test_token_with_broader_scope_refused(tmp_path):
    p = _token(tmp_path, [*GMAIL_SCOPES, "https://mail.google.com/"])
    with pytest.raises(PipelineError) as ei:
        gauth.load_credentials(Settings(_env_file=None, gmail_token_file=p))
    assert "broader scopes" in ei.value.detail


def test_token_without_scopes_refused(tmp_path):
    p = _token(tmp_path, [])
    with pytest.raises(PipelineError):
        gauth.load_credentials(Settings(_env_file=None, gmail_token_file=p))


def test_expired_token_refresh_failure_is_auth_failure(tmp_path, monkeypatch):
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials

    def boom(self, request):
        raise RefreshError("invalid_grant")

    monkeypatch.setattr(Credentials, "refresh", boom)
    p = _token(tmp_path, list(GMAIL_SCOPES), expired=True)
    with pytest.raises(PipelineError) as ei:
        gauth.load_credentials(Settings(_env_file=None, gmail_token_file=p))
    assert ei.value.code == ErrorCode.GMAIL_AUTH_FAILED
    assert "ya29" not in str(ei.value) and "1//" not in str(ei.value)  # no token leakage


def test_valid_token_loads(tmp_path):
    p = _token(tmp_path, list(GMAIL_SCOPES))
    creds = gauth.load_credentials(Settings(_env_file=None, gmail_token_file=p))
    assert set(creds.scopes) == set(GMAIL_SCOPES)


def test_token_written_with_0600(tmp_path):
    p = tmp_path / "secrets" / "token.json"
    gauth.write_token(p, "{}")
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_oauth_flow_requires_client_file(tmp_path):
    s = Settings(_env_file=None, gmail_credentials_file=tmp_path / "missing.json")
    with pytest.raises(PipelineError) as ei:
        gauth.run_oauth_flow(s)
    assert "GMAIL_SETUP" in ei.value.detail


# ---------------------------------------------------------------- Gmail sender


MSG = OutgoingEmail(
    to="hod@college.example",
    subject="Re: Need laptops",
    body="Noted.",
    in_reply_to="<abc123@college.example>",
    thread_id="t1",
)


def test_gmail_sender_builds_threaded_reply():
    svc = _svc()
    assert GmailSender(svc).send(MSG) == "gmail-sent-1"
    body = svc.sent[0]
    assert body["threadId"] == "t1"
    raw = base64.urlsafe_b64decode(body["raw"]).decode()
    assert "In-Reply-To: <abc123@college.example>" in raw and "To: hod@college.example" in raw


@pytest.mark.parametrize(
    "error, expected",
    [
        (HttpErr(400), SendRejected),
        (HttpErr(403), SendRejected),
        (HttpErr(429), SendRejected),
        (HttpErr(500), SendOutcomeUnknown),
        (HttpErr(503), SendOutcomeUnknown),
        (TimeoutError("read"), SendOutcomeUnknown),
        (ConnectionResetError(), SendOutcomeUnknown),
    ],
)
def test_gmail_send_errors_map_to_safe_outcomes(error, expected):
    with pytest.raises(expected):
        GmailSender(_svc(send=error)).send(MSG)


def test_send_mode_gmail_without_token_sends_nothing(db, monkeypatch, tmp_path):
    from app.sending.factory import build_sender

    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("SEND_MODE", "gmail")
    monkeypatch.setenv("GMAIL_TOKEN_FILE", str(tmp_path / "none.json"))
    get_settings.cache_clear()
    with pytest.raises(PipelineError) as ei:
        build_sender()
    assert ei.value.code == ErrorCode.GMAIL_AUTH_FAILED
    assert_nothing_sent(db)


# ---------------------------------------------------------------- API


def test_gmail_sync_endpoint(client, monkeypatch):
    from app.api import gmail as gapi

    assert client.post("/gmail/sync", headers={"X-Admin-Name": "A"}).status_code == 409  # demo
    monkeypatch.setenv("DEMO_MODE", "false")
    get_settings.cache_clear()
    monkeypatch.setattr(
        gapi, "get_gmail_service_dep", lambda: _svc(messages={"g1": gmsg(raw_message())})
    )
    r = client.post("/gmail/sync", headers={"X-Admin-Name": "A"})
    assert r.status_code == 200 and r.json()["created"] == 1

    def auth_fail():
        raise PipelineError(ErrorCode.GMAIL_AUTH_FAILED, "no token")

    monkeypatch.setattr(gapi, "get_gmail_service_dep", auth_fail)
    r = client.post("/gmail/sync", headers={"X-Admin-Name": "A"})
    assert r.status_code == 502 and r.json()["error"] == "GMAIL_AUTH_FAILED"
