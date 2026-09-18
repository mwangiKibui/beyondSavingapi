import hashlib
import io
import re
from datetime import datetime

import pdfplumber

from app.parsers.base import ParsedTransaction

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
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    currency_match = _CURRENCY_RE.search(full_text)
    currency = currency_match.group(1) if currency_match else DEFAULT_CURRENCY

    lines = full_text.split("\n")

    raw_rows: list[dict] = []
    pending: list[str] = []
    for line in lines:
        stripped = line.strip()
        if _TOTAL_LINE_RE.match(_dedouble_line(stripped)):
            continue  # closing summary row, not a transaction - handled separately below

        match = _DATA_LINE_RE.match(stripped)
        if not match:
            if stripped:
                pending.append(stripped)
            continue

        prefix = match.group("prefix").strip()
        detail_parts = pending + ([prefix] if prefix else [])
        raw_rows.append(
            {
                "reference": match.group("reference"),
                "date": match.group("date"),
                "balance": _parse_number(match.group("balance")),
                "description": " ".join(detail_parts),
            }
        )
        pending = []

    total_match = next(
        (m for line in lines if (m := _TOTAL_LINE_RE.match(_dedouble_line(line.strip())))), None
    )

    transactions: list[ParsedTransaction] = []
    # Already chronological (oldest first) in the source - no reversal needed,
    # matching Mentor Sacco/NCBA's row order (M-Pesa is the odd one out).
    previous_balance = round(
        _parse_number(total_match.group("closing_balance"))
        - _parse_number(total_match.group("total_credit"))
        + _parse_number(total_match.group("total_debit")),
        2,
    ) if total_match else (raw_rows[0]["balance"] if raw_rows else 0.0)

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

    return transactions
