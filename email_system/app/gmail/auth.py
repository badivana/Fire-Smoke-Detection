"""Gmail OAuth 2.0 (installed-app flow). Least privilege: gmail.readonly + gmail.send only.

    python -m app.gmail auth    # one-time: opens a browser, writes secrets/token.json (0600)

The token grants access to the mailbox: it is never logged, never committed
(secrets/ is git-ignored) and is refused if Google granted any broader scope.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from app.core.config import GMAIL_SCOPES, Settings
from app.core.errors import ErrorCode, PipelineError


def _auth_error(msg: str) -> PipelineError:
    return PipelineError(ErrorCode.GMAIL_AUTH_FAILED, msg)


def load_credentials(settings: Settings):
    """Load and (if needed) refresh the stored token. Raises GMAIL_AUTH_FAILED."""
    from google.auth.exceptions import GoogleAuthError, RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    path: Path = settings.gmail_token_file
    if not path.is_file():
        raise _auth_error("no Gmail token; run `python -m app.gmail auth`")
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        # Check the scopes RECORDED IN THE TOKEN: passing scopes= to the constructor would
        # make creds.scopes echo our request instead of what Google granted.
        granted = set(info.get("scopes") or [])
        creds = Credentials.from_authorized_user_info(info)
    except (ValueError, KeyError, AttributeError, GoogleAuthError):
        raise _auth_error(
            "Gmail token file is invalid; re-run `python -m app.gmail auth`"
        ) from None
    if not granted or not granted <= set(GMAIL_SCOPES):
        raise _auth_error("token has missing or broader scopes than allowed; re-authorize")
    if not creds.valid:
        if not (creds.expired and creds.refresh_token):
            raise _auth_error("Gmail token expired and cannot be refreshed; re-authorize")
        try:
            creds.refresh(Request())
        except (RefreshError, GoogleAuthError, OSError) as exc:
            raise _auth_error(
                f"token refresh failed ({type(exc).__name__}); re-authorize"
            ) from None
        write_token(path, creds.to_json())
    return creds


def write_token(path: Path, token_json: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token_json)
    os.chmod(path, 0o600)


def run_oauth_flow(settings: Settings) -> Path:
    """Interactive one-time consent in the browser (desktop OAuth client)."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not settings.gmail_credentials_file.is_file():
        raise _auth_error(
            f"OAuth client file not found: {settings.gmail_credentials_file} "
            "(download it from Google Cloud Console, see docs/GMAIL_SETUP.md)"
        )
    flow = InstalledAppFlow.from_client_secrets_file(
        str(settings.gmail_credentials_file), scopes=list(GMAIL_SCOPES)
    )
    creds = flow.run_local_server(port=0, open_browser=True)
    granted = set(creds.scopes or [])
    if not granted <= set(GMAIL_SCOPES):
        raise _auth_error("Google granted broader scopes than requested; token not saved")
    missing = set(GMAIL_SCOPES) - granted
    if missing:
        raise _auth_error(f"consent screen did not grant: {sorted(missing)}; token not saved")
    write_token(settings.gmail_token_file, creds.to_json())
    return settings.gmail_token_file


def build_service(creds):
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=creds, cache_discovery=False)
