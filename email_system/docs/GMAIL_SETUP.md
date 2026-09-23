# Gmail OAuth setup

Status: the code is written and tested against a **mocked** Gmail API. It has **not
been run against a real Gmail account** (none was available while building it). Do the
first run with a test mailbox.

The app asks for exactly two scopes:
- `gmail.readonly`: read messages and attachments;
- `gmail.send`: send approved replies.

It cannot delete, modify, label or read settings. A token with any other scope is
refused.

## 1. Google Cloud project (once)

1. Open https://console.cloud.google.com/ and create a project, e.g. `email-review-desk`.
2. **APIs & Services → Library → Gmail API → Enable**.
3. **APIs & Services → OAuth consent screen**:
   - User type: **Internal** if you use Google Workspace for the institution (only your
     domain can authorise). Otherwise **External**, and add your address as a **test
     user**.
   - Scopes: add `.../auth/gmail.readonly` and `.../auth/gmail.send`. Nothing else.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type: **Desktop app**.
   - Download the JSON file.

## 2. Put the client file in place (never commit it)

```zsh
cd email_system
mkdir -p secrets && chmod 700 secrets
mv ~/Downloads/client_secret_*.json secrets/credentials.json
chmod 600 secrets/credentials.json
```

`secrets/`, `credentials.json` and `token.json` are in `.gitignore`.

## 3. Authorise (once)

```zsh
source .venv/bin/activate
python -m app.gmail auth
```

A browser opens. Log in to the **institution's IT/Admin mailbox** and accept.
`secrets/token.json` is written with permissions `0600`. If Google granted fewer or
more scopes than the two above, the token is not saved.

## 4. Switch from demo to Gmail

In `.env`:

```env
DEMO_MODE=false
SEND_MODE=gmail            # or keep "simulated" at first: fetch real mail, send nothing
GMAIL_QUERY=in:inbox -in:spam newer_than:7d
ADMIN_API_KEY=<python -c "import secrets;print(secrets.token_urlsafe(32))">
```

Recommended order: first run with `SEND_MODE=simulated` or `disabled`, and check the
queue and drafts on real mail. Only then switch to `gmail`.

## 5. Fetch mail

```zsh
python -m app.gmail sync            # or POST /gmail/sync (needs X-Admin-Name)
```

Then process emails from the dashboard. Replies are sent with `In-Reply-To` and the
Gmail `threadId`, so they appear in the original thread.

## Revoking access

- Google Account → Security → Third-party access → remove the app.
- Delete `secrets/token.json`.

Without a valid token, fetching fails with `GMAIL_AUTH_FAILED` and sending is refused.
Nothing is sent.

## What happens on errors

| Situation | Result |
|---|---|
| Token missing, expired and not refreshable, or wrong scopes | `GMAIL_AUTH_FAILED`; nothing fetched, nothing sent |
| Gmail rejects a send (HTTP 400/401/403/404/429) | `FAILED` / `SEND_FAILED`: not delivered, retry allowed |
| Timeout, connection drop, HTTP 5xx during send | `FAILED` / `SEND_DELIVERY_UNKNOWN`: may have been delivered; retry and edits are blocked until someone checks Gmail's Sent folder |
