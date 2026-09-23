"""Content-addressed attachment storage.

Files are stored as <dir>/<sha[:2]>/<sha>.bin, so the sender-controlled filename never
touches the filesystem path (no path traversal), and identical files are stored once.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

_UNSAFE = re.compile(r"[\x00-\x1f\x7f<>:\"|?*]")


def sanitize_filename(name: str) -> str:
    base = re.split(r"[\\/]", name)[-1]
    base = _UNSAFE.sub("", base).strip().lstrip(".").strip()
    if not base:
        return "attachment"
    if len(base) > 255:
        stem, dot, ext = base.rpartition(".")
        base = (stem[: 254 - len(ext)] + dot + ext) if dot and len(ext) <= 10 else base[:255]
    return base


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def store_attachment(root: Path, data: bytes, digest: str) -> Path:
    target = root / digest[:2] / f"{digest}.bin"
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, target)  # atomic: readers never see a half-written file
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target
