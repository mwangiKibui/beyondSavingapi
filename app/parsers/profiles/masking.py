"""ab-314: a masked copy of a statement page for the AI layout proposer.

The model sees the page's LAYOUT, never its contents. Two things pass
through verbatim:

- the table's heading line(s), found by the same structural rule as the
  fingerprint (a digit-free line spanning the page over the money columns,
  so a name or address line can't be one);
- words from a fixed list of statement vocabulary ("Opening", "Balance",
  "Total", "Paid", "In", "Charges"...), so labels like "Opening Balance"
  or "Total Paid In" stay readable.

Every other word is reduced to its shape: digits -> 9, capitals -> X,
lower case -> x, punctuation kept ("03-09-26" -> "99-99-99",
"Kamau" -> "Xxxxx", "2,000.00" -> "9,999.99"). A name is neither a heading
nor in the list, so it can only ever leave as "Xxxx Xxxxx".

Words keep their horizontal position (scaled to a fixed width) so the
columns still line up for the model.
"""

import io
import re

import pdfplumber

from app.parsers.profiles.fingerprint import _header_clusters, _header_index
from app.parsers.profiles.layout import Line, is_body_line, page_lines

# Characters per line the page width is scaled to.
WIDTH = 120
# Lines of a page sent at most.
MAX_LINES = 60

# Statement words that carry layout meaning and no personal detail. Lower
# case, compared with surrounding punctuation stripped.
VOCAB = frozenset(
    """
    account accounts amount amounts and available b/f balance balances bank brought c/f carried charge charges
    closing cr credit credits currency date dates debit debits deposit deposits description details dr
    end fee fees for forward from in kes ksh money name narration narrative no number of opening out paid
    page period received reference ref sent start statement status summary tax to total totals
    transaction transactions txn type usd value withdrawal withdrawals
    """.split()
)

_STRIP = re.compile(r"^[^\w/]+|[^\w/]+$")


def shape(text: str) -> str:
    """'03-09-26' -> '99-99-99', 'Kamau' -> 'Xxxxx', '2,000.00' -> '9,999.99'."""
    out = []
    for ch in text:
        if ch.isdigit():
            out.append("9")
        elif ch.isalpha():
            out.append("X" if ch.isupper() else "x")
        else:
            out.append(ch)
    return "".join(out)


def _masked_word(text: str) -> str:
    core = _STRIP.sub("", text).lower()
    if core and core in VOCAB:
        return text
    return shape(text)


def _render(words: list[tuple[float, str]], page_width: float) -> str:
    """Words placed at their scaled x positions, at least one space apart."""
    line = ""
    for x0, text in words:
        col = round(x0 / page_width * WIDTH) if page_width else len(line)
        if line and col <= len(line):
            col = len(line) + 1
        line = line.ljust(col) + text
    return line.rstrip()


def _heading_lines(lines: list[Line], page_width: float) -> set[int]:
    body = next((i for i, line in enumerate(lines) if is_body_line(line)), None)
    if body is None:
        return set()
    at = _header_index(lines, body, page_width)
    if at is None:
        return set()
    out = {at}
    # A wrapped heading ("Value / Date"): the digit-free lines _header_clusters folded in.
    head_words = {id(w) for c in _header_clusters(lines, at, body) for w in c.words}
    for i in range(at + 1, body):
        if lines[i].words and all(id(w) in head_words for w in lines[i].words):
            out.add(i)
        else:
            break
    return out


def mask_page(page) -> str:
    """One pdfplumber page as masked text, one output line per text line."""
    lines = page_lines(page)
    width = float(page.width)
    headings = _heading_lines(lines, width)
    out = []
    for i, line in enumerate(lines[:MAX_LINES]):
        words = [(w.x0, w.text if i in headings else _masked_word(w.text)) for w in line.words]
        out.append(_render(words, width))
    return "\n".join(out)


def mask_pdf(content: bytes, *, pages: int = 1) -> str | None:
    """The first `pages` pages masked, or None for something we can't read
    (not a PDF, no text layer). Pages are separated by a marker line."""
    if not content.startswith(b"%PDF"):
        return None
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            texts = [mask_page(page) for page in pdf.pages[:pages] if page.extract_words()]
    except Exception:
        return None
    if not texts:
        return None
    return "\n".join(f"--- page {n} ---\n{text}" for n, text in enumerate(texts, start=1))


# --- CSV / XLSX (ab-319 grids) --------------------------------------------------

MAX_GRID_ROWS = 40


def _masked_cell(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "strftime"):  # a real spreadsheet date
        return "9999-99-99"
    if isinstance(value, bool):
        return "x"
    if isinstance(value, (int, float)):
        return shape(f"{value}")
    return " ".join(_masked_word(w) for w in str(value).split())


def mask_grid(grid: list[list]) -> str | None:
    """A spreadsheet's first rows masked, cells separated by ' | '. The
    heading row (digit-free, found the same way as the guess) stays
    verbatim; every other cell is reduced to its shape."""
    from app.parsers.profiles.guess import _grid_header

    if not grid:
        return None
    header_at = _grid_header(grid)
    out = []
    for i, row in enumerate(grid[:MAX_GRID_ROWS]):
        if i == header_at:
            cells = ["" if v is None else " ".join(str(v).split()) for v in row]
        else:
            cells = [_masked_cell(v) for v in row]
        out.append(" | ".join(cells).rstrip(" |"))
    return "\n".join(out)
