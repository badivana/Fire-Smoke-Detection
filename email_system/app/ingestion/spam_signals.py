"""Deterministic spam / phishing / injection signals (no LLM involved).

Signals are hints for the admin. Some of them also add a *review reason*, which forces
manual review. None of them can delete, auto-categorise, or send anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from email.utils import parseaddr

from app.core.categories import SpamSignals
from app.ingestion.normalize import for_matching

_ADDR_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")

DANGEROUS_EXTENSIONS = frozenset(
    {
        "exe",
        "scr",
        "js",
        "jse",
        "vbs",
        "vbe",
        "bat",
        "cmd",
        "com",
        "msi",
        "ps1",
        "jar",
        "hta",
        "html",
        "htm",
        "lnk",
        "iso",
        "img",
        "docm",
        "xlsm",
        "pptm",
        "wsf",
        "apk",
    }
)
_MAGIC = {
    "pdf": (b"%PDF-",),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpg": (b"\xff\xd8\xff",),
    "jpeg": (b"\xff\xd8\xff",),
}
_MIME_TO_EXT = {"application/pdf": "pdf", "image/png": "png", "image/jpeg": "jpg"}


@dataclass
class Signals:
    signals: list[str] = field(default_factory=list)
    review_reasons: list[str] = field(default_factory=list)

    def add(self, signal: str, review_reason: str | None = None) -> None:
        if signal not in self.signals:
            self.signals.append(signal)
        if review_reason and review_reason not in self.review_reasons:
            self.review_reasons.append(review_reason)


@dataclass(frozen=True)
class SenderInfo:
    address: str
    name: str | None
    domain: str | None
    valid: bool


def parse_sender(raw: str) -> SenderInfo:
    name, addr = parseaddr(raw)
    addr = addr.strip().lower()
    valid = bool(_ADDR_RE.match(addr))
    domain = addr.rsplit("@", 1)[1] if valid else None
    return SenderInfo(
        address=addr or raw.strip()[:320], name=name or None, domain=domain, valid=valid
    )


def _domain_trusted(domain: str, trusted: tuple[str, ...]) -> bool:
    return any(domain == t or domain.endswith("." + t) for t in trusted)


def header_signals(
    sig: Signals,
    cfg: SpamSignals,
    sender: SenderInfo,
    headers: dict[str, str],
    labels: list[str],
) -> None:
    h = {k.lower(): v for k, v in headers.items()}
    for label in labels:
        if label.upper() in {x.upper() for x in cfg.gmail_labels}:
            sig.add(f"gmail_label:{label.upper()}", "Gmail marked this email as spam")
    for name in cfg.bulk_headers:
        if name.lower() in h:
            sig.add(f"bulk_header:{name}")
    precedence = h.get("precedence", "").strip().lower()
    if precedence and precedence in cfg.precedence_values:
        sig.add(f"precedence:{precedence}")
    if not sender.valid:
        sig.add("invalid_sender", "Sender address could not be parsed")
    elif cfg.trusted_sender_domains and not _domain_trusted(
        sender.domain or "", cfg.trusted_sender_domains
    ):
        sig.add("external_sender")
    reply_to = h.get("reply-to")
    if reply_to and sender.domain:
        rt = parse_sender(reply_to)
        if rt.domain and rt.domain != sender.domain:
            sig.add("reply_to_mismatch")


def content_signals(
    sig: Signals,
    cfg: SpamSignals,
    *,
    subject: str,
    body: str,
    hidden_text: str,
    invisible_chars: int,
) -> None:
    if hidden_text:
        sig.add("hidden_html_text", f"Email contains hidden text ({len(hidden_text)} chars)")
    if invisible_chars:
        sig.add(f"invisible_chars:{invisible_chars}")
    haystack = for_matching(f"{subject}\n{body}\n{hidden_text}")
    for phrase in cfg.suspicious_phrases:
        if phrase in haystack:
            sig.add(
                f"suspicious_phrase:{phrase}", "Possible phishing or prompt-injection text detected"
            )


def attachment_signals(sig: Signals, filename: str, mime_type: str, data: bytes) -> None:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in DANGEROUS_EXTENSIONS:
        sig.add(f"dangerous_attachment:{filename}", f"Dangerous attachment type: {filename}")
    expected = ext if ext in _MAGIC else _MIME_TO_EXT.get(mime_type.lower())
    if expected and not data.startswith(_MAGIC[expected]):
        sig.add(
            f"attachment_type_mismatch:{filename}",
            f"Attachment content does not match its type: {filename}",
        )
