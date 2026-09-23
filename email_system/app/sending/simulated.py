"""Demo-mode sender: writes the reply as an .eml file to a local outbox. Nothing leaves
the machine."""

from __future__ import annotations

import uuid
from email.message import EmailMessage
from pathlib import Path

from app.sending.base import EmailSender, OutgoingEmail

SIMULATED_FROM = "it-admin@demo.local"


class SimulatedSender(EmailSender):
    name = "simulated"

    def __init__(self, outbox_dir: Path) -> None:
        self.outbox_dir = outbox_dir

    def send(self, message: OutgoingEmail) -> str:
        msg_id = f"sim-{uuid.uuid4().hex}"
        m = EmailMessage()
        m["From"] = SIMULATED_FROM
        m["To"] = message.to
        m["Subject"] = message.subject
        m["Message-ID"] = f"<{msg_id}@demo.local>"
        if message.in_reply_to:
            m["In-Reply-To"] = message.in_reply_to
            m["References"] = message.in_reply_to
        m["X-Simulated"] = "true (demo mode, not delivered)"
        m.set_content(message.body)
        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        (self.outbox_dir / f"{msg_id}.eml").write_bytes(bytes(m))
        return msg_id

    def sent_files(self) -> list[Path]:
        return sorted(self.outbox_dir.glob("*.eml")) if self.outbox_dir.exists() else []
