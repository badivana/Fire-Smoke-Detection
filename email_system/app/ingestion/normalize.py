"""Text cleaning. Everything here treats email content as untrusted data.

- HTML is converted to text with the stdlib parser; scripts/styles are dropped and
  text hidden with CSS (a common prompt-injection trick) is separated out so it can be
  flagged, not silently fed to the LLM.
- Invisible/bidi control characters are removed (they can hide or reorder words).
- NFKC folds look-alike forms (e.g. full-width letters) so phrase checks can't be dodged.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser

_INVISIBLE = re.compile("[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MANY_BLANK_LINES = re.compile(r"\n{3,}")
_TRAILING_SPACE = re.compile(r"[ \t]+\n")
_WS = re.compile(r"\s+")
_HIDDEN_STYLE = re.compile(
    r"display:none|visibility:hidden|font-size:0(?:px|pt|em|rem|%)?(?:;|$)"
    r"|opacity:0(?:\.0+)?(?:;|$)|max-height:0(?:px)?(?:;|$)"
)

_VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_SKIP = {"script", "style", "head", "title", "noscript", "template"}
_BLOCK = {
    "p",
    "div",
    "br",
    "li",
    "tr",
    "table",
    "ul",
    "ol",
    "section",
    "article",
    "header",
    "footer",
    "blockquote",
    "pre",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
}


@dataclass(frozen=True)
class CleanResult:
    text: str
    invisible_chars_removed: int


@dataclass(frozen=True)
class HtmlResult:
    text: str
    hidden_text: str  # text a human reader would not see


class _HtmlToText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.hidden: list[str] = []
        self.stack: list[tuple[str, bool, bool]] = []  # (tag, css_hidden, skipped)

    @staticmethod
    def _css_hidden(attrs: list[tuple[str, str | None]]) -> bool:
        for name, value in attrs:
            if name == "hidden":
                return True
            if name == "style" and value:
                if _HIDDEN_STYLE.search(value.lower().replace(" ", "")):
                    return True
        return False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK:
            self.out.append("\n")
        if tag in _VOID:
            return
        self.stack.append((tag, self._css_hidden(attrs), tag in _SKIP))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK:
            self.out.append("\n")
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data: str) -> None:
        if any(skipped for _, _, skipped in self.stack):
            return
        if any(hidden for _, hidden, _ in self.stack):
            if data.strip():
                self.hidden.append(data.strip())
            return
        self.out.append(data)


def html_to_text(html: str) -> HtmlResult:
    parser = _HtmlToText()
    parser.feed(html)
    parser.close()
    return HtmlResult(text="".join(parser.out), hidden_text=" ".join(parser.hidden))


def clean_text(text: str) -> CleanResult:
    text = unicodedata.normalize("NFKC", text)
    body = text[1:] if text.startswith("\ufeff") else text
    invisible = len(_INVISIBLE.findall(body))
    text = _INVISIBLE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    text = _CONTROL.sub("", text)
    text = _TRAILING_SPACE.sub("\n", text)
    text = _MANY_BLANK_LINES.sub("\n\n", text)
    return CleanResult(text=text.strip(), invisible_chars_removed=invisible)


def clean_line(text: str, limit: int) -> str:
    """Single-line field (subject, name): cleaned, whitespace collapsed, length capped."""
    return _WS.sub(" ", clean_text(text).text).strip()[:limit]


def for_matching(text: str) -> str:
    """Lower-case, whitespace-collapsed form used only for phrase detection."""
    return _WS.sub(" ", clean_text(text).text.lower())


def truncate(text: str, limit: int) -> tuple[str, bool]:
    """Cut to `limit` chars, preferring a line break near the end, with a visible marker."""
    if len(text) <= limit:
        return text, False
    cut = text.rfind("\n", int(limit * 0.9), limit)
    cut = cut if cut != -1 else limit
    return f"{text[:cut].rstrip()}\n[... truncated {len(text) - cut} characters ...]", True
