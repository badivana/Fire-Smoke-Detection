"""Regenerate the demo PDF attachments in app/demo/fixtures/.

    pip install -r requirements-dev.txt
    python scripts/make_demo_fixtures.py

- quotation_acme.pdf   : normal PDF with a text layer (PyMuPDF can read it)
- quotation_scanned.pdf: image-only PDF, no text layer (needs OCR, Phase 10)
All names, companies and numbers are fictional (.example domains).
"""

from __future__ import annotations

import io
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).resolve().parents[1] / "app" / "demo" / "fixtures"

ACME_LINES = [
    "ACME COMPUTERS PVT. LTD.",
    "12 Market Road, Pune 411001  |  sales@acme-computers.example",
    "",
    "QUOTATION",
    "Quotation No: QTN-2026-0412          Date: 18-09-2026",
    "To: Purchase Section, Greenfield Institute of Technology",
    "",
    "Sl  Item                                     Qty   Unit Price (INR)   Amount (INR)",
    "1   Desktop PC (Intel i5-13400, 16GB,         20       52,000.00      10,40,000.00",
    "    512GB NVMe SSD, 23.8in monitor)",
    "2   UPS 1 kVA line-interactive                20        4,500.00         90,000.00",
    "",
    "                                         Sub-total              11,30,000.00",
    "                                         GST @ 18%               2,03,400.00",
    "                                         GRAND TOTAL            13,33,400.00",
    "",
    "Validity: 30 days from the date of quotation.",
    "Delivery: within 3 weeks of purchase order, free delivery to campus.",
    "Warranty: 3 years onsite.",
    "Payment terms: 100% within 30 days of delivery.",
]

SCANNED_LINES = [
    "BRIGHTLINE PROJECTORS",
    "44 Station Lane, Nagpur  -  brightline-av.example",
    "",
    "QUOTATION  No. BL/Q/2291     Date 19/09/2026",
    "",
    "Item                               Qty    Rate        Amount",
    "LCD Projector 4000 lumens, WXGA     6    38,500     2,31,000",
    "Ceiling mount kit                   6     2,200        13,200",
    "",
    "Sub total                                        2,44,200",
    "GST 18%                                            43,956",
    "TOTAL                                           2,88,156",
    "",
    "Valid for 15 days. Delivery 10 days after PO.",
]


def text_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)  # A4
    y = 60
    for i, line in enumerate(ACME_LINES):
        size = 16 if i in (0, 3) else 9.5
        page.insert_text((50, y), line, fontsize=size, fontname="cour")
        y += size + 8
    doc.set_metadata({"title": "Quotation QTN-2026-0412", "author": "Acme Computers (fictional)"})
    doc.save(path, garbage=4, deflate=True)


def scanned_pdf(path: Path) -> None:
    w, h = 1240, 1754  # A4 at 150 dpi
    img = Image.new("L", (w, h), 250)
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=26)
    big = ImageFont.load_default(size=40)
    y = 120
    for i, line in enumerate(SCANNED_LINES):
        f = big if i == 0 else font
        draw.text((110, y), line, fill=25, font=f)
        y += 58 if i == 0 else 42
    # Look like a scan: slight rotation + blur.
    img = img.rotate(0.6, expand=False, fillcolor=250).filter(ImageFilter.GaussianBlur(0.6))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=60)
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(page.rect, stream=buf.getvalue())
    doc.save(path, garbage=4, deflate=True)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    text_pdf(OUT / "quotation_acme.pdf")
    scanned_pdf(OUT / "quotation_scanned.pdf")
    for p in sorted(OUT.glob("*.pdf")):
        print(f"{p.name}: {p.stat().st_size} bytes")
