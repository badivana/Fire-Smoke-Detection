"""Attachment text extraction: PDF text layer with PyMuPDF, Tesseract OCR only when a PDF
page or an image has no usable text layer.

Attachments are untrusted input:
  - page count, render size and OCR time are capped (decompression-bomb / DoS limits)
  - encrypted PDFs are not opened
  - Tesseract runs as a subprocess with stdin/stdout only (no shell, no temp files)
  - the resulting text is treated like the email body: cleaned, truncated, scanned for
    injection phrases, and only ever passed to the LLM inside the untrusted block
"""

from __future__ import annotations

import shutil

# subprocess is only used to run Tesseract (ocr_png): fixed argv, no shell
import subprocess  # nosec B404
from dataclasses import dataclass

import pymupdf

from app.db.base import TextSource
from app.ingestion.normalize import clean_text, truncate

MAX_PAGES = 20
OCR_DPI = 200
MAX_RENDER_PIXELS = 40_000_000  # ~A3 at 300 dpi; larger pages are downscaled
MIN_TEXT_CHARS_PER_PAGE = 25  # below this a page is treated as scanned
OCR_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class ExtractResult:
    text: str | None
    source: TextSource
    page_count: int | None
    truncated: bool
    error: str | None


class OCRUnavailable(Exception):
    pass


def tesseract_path(configured: str | None) -> str | None:
    return configured or shutil.which("tesseract")


def ocr_png(png: bytes, cmd: str) -> str:
    try:
        # fixed argv, no shell, image via stdin, timeout: nothing from the email reaches argv
        r = subprocess.run(  # nosec B603  # noqa: S603
            [cmd, "stdin", "stdout", "-l", "eng", "--psm", "6"],
            input=png,
            capture_output=True,
            timeout=OCR_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise OCRUnavailable(str(exc)) from None
    if r.returncode != 0:
        raise RuntimeError(f"tesseract exit {r.returncode}")
    return r.stdout.decode("utf-8", errors="replace")


def _render(page: pymupdf.Page) -> bytes:
    zoom = OCR_DPI / 72
    w, h = page.rect.width * zoom, page.rect.height * zoom
    if w * h > MAX_RENDER_PIXELS:
        zoom *= (MAX_RENDER_PIXELS / (w * h)) ** 0.5
    return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes("png")


def _finish(
    parts: list[str],
    source: TextSource,
    pages: int | None,
    max_chars: int,
    error: str | None = None,
) -> ExtractResult:
    text, cut = truncate(clean_text("\n\n".join(p for p in parts if p.strip())).text, max_chars)
    return ExtractResult(text or None, source if text else TextSource.NONE, pages, cut, error)


def extract_pdf(
    data: bytes, *, max_chars: int, ocr_cmd: str | None, ocr_enabled: bool
) -> ExtractResult:
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001  corrupt/unsupported file
        return ExtractResult(
            None, TextSource.NONE, None, False, f"cannot open PDF ({type(exc).__name__})"
        )
    with doc:
        if doc.needs_pass or doc.is_encrypted:
            return ExtractResult(
                None, TextSource.NONE, doc.page_count, False, "encrypted PDF not opened"
            )
        pages = list(doc)[:MAX_PAGES]
        texts = [p.get_text() for p in pages]
        scanned = [i for i, t in enumerate(texts) if len(t.strip()) < MIN_TEXT_CHARS_PER_PAGE]
        note = f"only first {MAX_PAGES} pages read" if doc.page_count > MAX_PAGES else None
        if not scanned:
            return _finish(texts, TextSource.TEXT_LAYER, doc.page_count, max_chars, note)
        if not ocr_enabled or not ocr_cmd:
            if len(scanned) < len(pages):  # partial text layer is still useful
                return _finish(
                    texts,
                    TextSource.TEXT_LAYER,
                    doc.page_count,
                    max_chars,
                    "some pages are scanned and OCR is not available",
                )
            return ExtractResult(
                None,
                TextSource.NONE,
                doc.page_count,
                False,
                "scanned PDF and OCR not available (install Tesseract)",
            )
        try:
            for i in scanned:
                texts[i] = ocr_png(_render(pages[i]), ocr_cmd)
        except OCRUnavailable:
            return ExtractResult(
                None,
                TextSource.NONE,
                doc.page_count,
                False,
                "OCR not available (install Tesseract)",
            )
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            return ExtractResult(
                None, TextSource.NONE, doc.page_count, False, f"OCR failed ({type(exc).__name__})"
            )
        return _finish(texts, TextSource.OCR, doc.page_count, max_chars, note)


def extract_image(
    data: bytes, *, max_chars: int, ocr_cmd: str | None, ocr_enabled: bool
) -> ExtractResult:
    if not ocr_enabled or not ocr_cmd:
        return ExtractResult(None, TextSource.NONE, None, False, "OCR not available")
    try:
        pix = pymupdf.Pixmap(data)
        if pix.width * pix.height > MAX_RENDER_PIXELS:
            return ExtractResult(None, TextSource.NONE, None, False, "image too large for OCR")
        return _finish([ocr_png(pix.tobytes("png"), ocr_cmd)], TextSource.OCR, 1, max_chars)
    except OCRUnavailable:
        return ExtractResult(None, TextSource.NONE, None, False, "OCR not available")
    except Exception as exc:  # noqa: BLE001  corrupt image / OCR error
        return ExtractResult(
            None, TextSource.NONE, None, False, f"image not readable ({type(exc).__name__})"
        )


def extract_text(
    data: bytes, *, max_chars: int, ocr_cmd: str | None, ocr_enabled: bool
) -> ExtractResult:
    """Dispatch on file CONTENT (magic bytes), never on the sender-supplied name/type."""
    kw = {"max_chars": max_chars, "ocr_cmd": ocr_cmd, "ocr_enabled": ocr_enabled}
    if data.startswith(b"%PDF-"):
        return extract_pdf(data, **kw)
    if data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
        return extract_image(data, **kw)
    if data.startswith((b"\xef\xbb\xbf",)) or _looks_like_text(data):
        text, cut = truncate(clean_text(data.decode("utf-8", errors="replace")).text, max_chars)
        return ExtractResult(text or None, TextSource.TEXT_LAYER, None, cut, None)
    return ExtractResult(None, TextSource.NONE, None, False, "unsupported attachment type")


def _looks_like_text(data: bytes) -> bool:
    sample = data[:4096]
    if not sample or b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True
