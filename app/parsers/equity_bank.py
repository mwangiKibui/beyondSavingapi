import hashlib
import io
import re
from datetime import datetime

import pdfplumber

from app.parsers.base import ParsedTransaction, StatementParseError, validate_running_balance

DEFAULT_CURRENCY = "KES"

# A transaction's numeric line: some optional wrapped-description text,
# then the payment reference (a single token - no spaces), a date, an
# amount, and the running balance. Only one of Credit/Debit is ever
# populated per row, and it renders as a single bare number here - which
# column it came from isn't recoverable from text alone, so direction is
# derived from the balance delta instead (see _direction_and_amount).
_DATA_LINE_RE = re.compile(
    r"^(?P<prefix>.*?)\s*(?P<reference>\S+)\s+(?P<date>\d{2}/\d{2}/\d{4})"
    r"\s+(?P<amount>[\d,]+\.\d{2})\s+(?P<balance>[\d,]+\.\d{2})$"
)

# The closing summary row - also gives the statement's opening balance
# (closing - total_credit + total_debit), since there's no explicit
# "Opening Balance" field anywhere else in this provider's statement.
# The whole line sometimes renders with every character doubled (see
# _dedouble_line) depending on how bold that template row is.
_TOTAL_LINE_RE = re.compile(
    r"^Total\s+(?P<total_credit>[\d,]+\.\d{2})\s+(?P<total_debit>[\d,]+\.\d{2})"
    r"\s+(?P<closing_balance>[\d,]+\.\d{2})$",
    re.IGNORECASE,
)

_CURRENCY_RE = re.compile(r"Currency\s+([A-Z]{3})")

# The table's column-heading line (often rendered with doubled letters).
# Everything above it on a page is the statement/page header, never part
# of a transaction (ab-272).
_HEADING_RE = re.compile(r"^Transaction Details\b.*\bBalance$", re.IGNORECASE)

# Page footer lines - the table ends at the first one on each page.
_FOOTER_RE = re.compile(r"^(Disclaimer:|.*\bPage \d+/\d+$)", re.IGNORECASE)

# One text line on a page: (vertical centre, text).
Line = tuple[float, str]


def _dedouble_word(word: str) -> str:
    """Undoes a faux-bold rendering trick this template uses for some
    labels/totals - every character in the word rendered twice (e.g.
    "TToottaall" for "Total"). Real transaction data is never affected,
    only certain header/label/total text - and only words that are
    genuinely fully doubled get touched here.
    """
    if len(word) < 2 or len(word) % 2 != 0:
        return word
    if all(word[i] == word[i + 1] for i in range(0, len(word), 2)):
        return word[::2]
    return word


def _dedouble_line(line: str) -> str:
    return " ".join(_dedouble_word(w) for w in line.split())


def _parse_number(value: str) -> float:
    return float(value.replace(",", ""))


def _dedupe_hash(*, reference: str, description: str, amount: float) -> str:
    fingerprint = f"{reference}|{description}|{amount:.2f}"
    return hashlib.sha256(fingerprint.encode()).hexdigest()


def parse_equity_bank_statement(content: bytes) -> list[ParsedTransaction]:
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        pages = [
            [((line["top"] + line["bottom"]) / 2, line["text"]) for line in page.extract_text_lines()]
            for page in pdf.pages
        ]
    return _parse_pages(pages)


def _parse_statement_text(full_text: str) -> list[ParsedTransaction]:
    """Plain-text entry point (one page, one unit of height per line)."""
    return _parse_pages([[(float(i), text) for i, text in enumerate(full_text.split("\n"))]])


def _table_lines(page: list[Line]) -> list[Line]:
    """The lines between a page's column heading and its Total/footer."""
    start = next(
        (i + 1 for i, (_, text) in enumerate(page) if _HEADING_RE.match(_dedouble_line(text.strip()))),
        0,
    )
    table: list[Line] = []
    for y, text in page[start:]:
        stripped = text.strip()
        if _TOTAL_LINE_RE.match(_dedouble_line(stripped)) or _FOOTER_RE.match(stripped):
            break
        if stripped:
            table.append((y, stripped))
    return table


def _rows_on_page(page: list[Line]) -> list[dict]:
    """Equity prints a row's details around its amounts line - some above,
    some below - so each detail line belongs to the amounts line nearest to
    it vertically (the earlier row on a tie), not simply the next one.
    """
    lines = _table_lines(page)
    data = {i: m for i, (_, text) in enumerate(lines) if (m := _DATA_LINE_RE.match(text))}
    if not data:
        return []

    parts: dict[int, list[Line]] = {i: [] for i in data}
    for i, (y, text) in enumerate(lines):
        if i in data:
            prefix = data[i].group("prefix").strip()
            if prefix:
                parts[i].append((y, prefix))
            continue
        owner = min(data, key=lambda d: (abs(lines[d][0] - y), d))
        parts[owner].append((y, text))

    return [
        {
            "reference": m.group("reference"),
            "date": m.group("date"),
            "balance": _parse_number(m.group("balance")),
            "description": " ".join(text for _, text in sorted(parts[i], key=lambda p: p[0])),
        }
        for i, m in data.items()
    ]


def _parse_pages(pages: list[list[Line]]) -> list[ParsedTransaction]:
    full_text = "\n".join(text for page in pages for _, text in page)
    currency_match = _CURRENCY_RE.search(full_text)
    currency = currency_match.group(1) if currency_match else DEFAULT_CURRENCY

    lines = full_text.split("\n")
    raw_rows = [row for page in pages for row in _rows_on_page(page)]

    total_match = next(
        (m for line in lines if (m := _TOTAL_LINE_RE.match(_dedouble_line(line.strip())))), None
    )
    if total_match is None:
        raise StatementParseError(
            "Could not find Equity Bank's closing 'Total' row - can't determine the statement's opening balance"
        )

    opening_balance = round(
        _parse_number(total_match.group("closing_balance"))
        - _parse_number(total_match.group("total_credit"))
        + _parse_number(total_match.group("total_debit")),
        2,
    )

    transactions: list[ParsedTransaction] = []
    # Already chronological (oldest first) in the source - no reversal needed,
    # matching Mentor Sacco/NCBA's row order (M-Pesa is the odd one out).
    previous_balance = opening_balance

    for row in raw_rows:
        # Rounded to 2dp at each step - subtracting two decimal floats
        # (e.g. 41340.99 - 340.99) doesn't always land on an exact
        # binary float, and amounts ultimately store as NUMERIC(18,2).
        amount = round(abs(row["balance"] - previous_balance), 2)
        direction = "in" if row["balance"] >= previous_balance else "out"
        previous_balance = row["balance"]

        transactions.append(
            {
                "txn_date": datetime.strptime(row["date"], "%d/%m/%Y").date(),
                "amount": amount,
                "currency": currency,
                "direction": direction,
                "counterparty": None,  # not mapped for MVP1 - see the runbook
                "description": row["description"],
                "balance_after": row["balance"],
                "dedupe_hash": _dedupe_hash(
                    reference=row["reference"], description=row["description"], amount=amount
                ),
            }
        )

    validate_running_balance(transactions, opening_balance=opening_balance)
    return transactions
