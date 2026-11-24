"""ab-312: a statement's page-1 fingerprint - the SHAPE of the layout,
used to find the profile that reads it.

It holds the table's heading words in order, how many columns there are
and where they start, the label words before a colon above the table
("Statement Period") and the title. Values are never taken, and the rules
are structural so a person's details can't get in:

- The heading row is the nearest digit-free line above the first
  transaction row (a line with a date and an amount) that spans at least
  3 columns across half the page and sits over that row's amounts. A
  name or address line is narrow and never over the money columns.
- A label is the text BEFORE the first ':' on a line above the table,
  only when it has no digits and at most 4 words.
- The title must be in the page's largest font near the top, digit-free,
  and contain a word like "statement" or "account".
- Every stored word passes the profile LayoutText rule (letters only).
"""

import io
import json
import re

import pdfplumber
from pydantic import BaseModel, ConfigDict

from app.parsers.profiles.layout import Line, clusters, is_body_line, money_words, page_lines
from app.parsers.profiles.text import normalise

_ALLOWED = re.compile(r"^[a-z][a-z .:/()&'%-]*$")
_TITLE_WORDS = {"statement", "e-statement", "account", "transactions", "transaction", "history", "report"}
X_BUCKETS = 40
MONEY_OVERLAP = 10.0


class PageFingerprint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    v: int = 1
    file_type: str = "pdf"
    title: list[str] = []
    labels: list[str] = []
    header: list[str] = []
    cols: int = 0
    # Each column's left edge, as a share of the page width in 40ths.
    x: list[int] = []

    def to_text(self) -> str:
        """Canonical compact JSON - what statement_profiles.fingerprint and
        statement_imports.fingerprint store."""
        return json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_text(cls, text: str) -> "PageFingerprint":
        return cls.model_validate(json.loads(text))


def _safe(text: str, *, max_words: int) -> str | None:
    text = normalise(text)
    if not text or len(text) > 40 or len(text.split()) > max_words or not _ALLOWED.match(text):
        return None
    return text


def _header_index(lines: list[Line], body: int, page_width: float) -> int | None:
    money = money_words(lines[body])
    for i in range(body - 1, -1, -1):
        line = lines[i]
        if line.has_digits:
            continue
        cl = clusters(line)
        if len(cl) < 3 or (cl[-1].x1 - cl[0].x0) < page_width * 0.5:
            continue
        over_money = sum(
            1 for c in cl if any(c.x0 - MONEY_OVERLAP <= m.x1 and m.x0 <= c.x1 + MONEY_OVERLAP for m in money)
        )
        if over_money >= 2:
            return i
    return None


def _header_clusters(lines: list[Line], index: int, body: int):
    head = clusters(lines[index])
    # A wrapped heading ("Value / Date"): a digit-free line just below whose
    # words all sit under one of the heading's columns.
    nxt = index + 1
    while nxt < body and not lines[nxt].has_digits:
        below = lines[nxt].words
        homes = []
        for word in below:
            home = next((c for c in head if c.x0 - 8 <= word.x0 <= c.x1 + 8), None)
            if home is None:
                break
            homes.append((home, word))
        else:
            for home, word in homes:
                home.words.append(word)
            nxt += 1
            continue
        break
    return head


def fingerprint_page(page) -> PageFingerprint:
    lines = page_lines(page)
    width = float(page.width)
    body = next((i for i, line in enumerate(lines) if is_body_line(line)), None)
    header_at = _header_index(lines, body, width) if body is not None else None

    header, xs = [], []
    if header_at is not None:
        for cluster in _header_clusters(lines, header_at, body):
            text = _safe(cluster.text, max_words=5)
            if text:
                header.append(text)
                xs.append(round(cluster.x0 / width * X_BUCKETS))
    above = lines[:header_at] if header_at is not None else lines[: body if body is not None else len(lines)]

    labels = set()
    for line in above:
        raw = line.text
        if ":" in raw:
            label = _safe(raw.split(":", 1)[0], max_words=4)
            if label and not any(ch.isdigit() for ch in raw.split(":", 1)[0]):
                labels.add(label)

    title = []
    chars = [c for c in page.chars if c["top"] < float(page.height) * 0.25]
    if chars:
        biggest = max(round(c["size"], 1) for c in chars)
        big_tops = {round(c["top"]) for c in chars if round(c["size"], 1) == biggest}
        for line in lines:
            if any(abs(line.top - t) <= 3 for t in big_tops) and not line.has_digits:
                text = _safe(line.text, max_words=6)
                if text and set(text.split()) & _TITLE_WORDS:
                    title.append(text)
                    break

    return PageFingerprint(
        title=title, labels=sorted(labels)[:20], header=header[:12], cols=len(header[:12]), x=xs[:12]
    )


def fingerprint_pdf(content: bytes) -> PageFingerprint | None:
    """Page 1's fingerprint, or None for something that isn't a PDF or a
    page with no text layer (a scan)."""
    if not content.startswith(b"%PDF"):
        return None
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            if not pdf.pages:
                return None
            page = pdf.pages[0]
            if not page.extract_words():
                return None
            return fingerprint_page(page)
    except Exception:
        return None
