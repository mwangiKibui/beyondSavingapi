import hashlib
import io
import re
from datetime import datetime
from typing import TypedDict

import pdfplumber

from app.parsers.base import ParsedTransaction

# Not stated anywhere in the statement - Mentor Sacco is Kenya-only.
CURRENCY = "KES"

# A single member statement bundles four independent sub-ledgers, each
# with its own opening balance and running balance - not sections of one
# ledger. "Instant Loan" can appear more than once (a pre-existing loan's
# amortization ledger, and a brand-new same-day disbursement are both
# named "Instant Loan" but are genuinely separate balance sequences) -
# each occurrence in the file starts its own SubLedgerStatement, never
# merged by name.
_SECTION_HEADERS = {"Ordinary Deposit", "Savings Account", "Share Capital", "Instant Loan"}

# pdfplumber's table detection is unreliable on this document (same
# fragmentation issue as Equity Bank) - parsed from raw text instead.
# A row is: date, then free-form text (a Document No token followed by
# the transaction details, all run together), then an amount, then a
# balance, optionally followed by a CR/DR mark. Only one of Debit/Credit
# is ever populated per row and - like every other bank/sacco provider
# seen so far - renders as a single bare number with no reliable way to
# tell which column it came from from text alone.
_DATA_LINE_RE = re.compile(
    r"^(?P<date>\d{2}-\d{2}-\d{4})\s+(?P<rest>.+?)\s+"
    r"(?P<amount>\(?[\d,]+\.\d{2}\)?)\s+(?P<balance>\(?[\d,]+\.\d{2}\)?)"
    r"(?:\s+(?:CR|DR))?$"
)


class SubLedgerStatement(TypedDict):
    name: str
    opening_balance: float
    transactions: list[ParsedTransaction]


def _parse_signed_number(value: str) -> float:
    # Negative balances are parenthesized, e.g. "(100.00)" - standard
    # accounting notation, not a formatting error.
    value = value.strip()
    negative = value.startswith("(") and value.endswith(")")
    if negative:
        value = value[1:-1]
    number = float(value.replace(",", ""))
    return -number if negative else number


def _dedupe_hash(*, section: str, description: str, amount: float) -> str:
    # Document No repeats across sub-ledgers for one real-world event
    # (e.g. a loan repayment posting to both the loan and savings
    # sub-ledgers) - not unique alone. It's also not cleanly isolated
    # from the rest of the line's text (see _DATA_LINE_RE), so rather
    # than trying to extract it separately, the full description
    # (which already contains it) plus the sub-ledger name plus amount
    # gives the same practical uniqueness the runbook calls for.
    fingerprint = f"{section}|{description}|{amount:.2f}"
    return hashlib.sha256(fingerprint.encode()).hexdigest()


def _build_transactions(
    rows: list[dict], *, opening_balance: float, has_explicit_opening: bool, section: str
) -> tuple[float, list[ParsedTransaction]]:
    transactions: list[ParsedTransaction] = []
    previous_balance = opening_balance

    for i, row in enumerate(rows):
        if i == 0 and not has_explicit_opening:
            # No stated "Opening Balance" line for this section (seen on
            # Savings Account, and on a fresh loan's disbursement row) -
            # back-derive an implicit opening assuming the first row is
            # an increase (a deposit or a disbursement, the common case
            # for a sub-ledger with nothing carried over to state). This
            # becomes the section's own reported opening_balance too.
            previous_balance = round(row["balance"] - row["amount"], 2)
            opening_balance = previous_balance

        amount = round(abs(row["balance"] - previous_balance), 2)
        direction = "in" if row["balance"] >= previous_balance else "out"
        previous_balance = row["balance"]

        transactions.append(
            {
                "txn_date": datetime.strptime(row["date"], "%d-%m-%Y").date(),
                "amount": amount,
                "currency": CURRENCY,
                "direction": direction,
                "counterparty": None,  # not mapped for MVP1 - see the runbook
                "description": row["description"],
                "balance_after": row["balance"],
                "dedupe_hash": _dedupe_hash(section=section, description=row["description"], amount=amount),
            }
        )

    return opening_balance, transactions


def parse_mentor_sacco_statement(content: bytes) -> list[SubLedgerStatement]:
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    sections: list[SubLedgerStatement] = []
    section_name: str | None = None
    opening_balance = 0.0
    has_explicit_opening = False
    rows: list[dict] = []

    def flush() -> None:
        if section_name is None:
            return
        effective_opening, transactions = _build_transactions(
            rows,
            opening_balance=opening_balance,
            has_explicit_opening=has_explicit_opening,
            section=section_name,
        )
        sections.append(
            {
                "name": section_name,
                "opening_balance": effective_opening,
                "transactions": transactions,
            }
        )

    for line in full_text.split("\n"):
        stripped = line.strip()

        if stripped in _SECTION_HEADERS:
            flush()
            section_name = stripped
            opening_balance = 0.0
            has_explicit_opening = False
            rows = []
            continue

        match = _DATA_LINE_RE.match(stripped)
        if not match or section_name is None:
            continue  # header/footer/page-break noise, or text before the first section

        rest = match.group("rest").strip()
        balance = _parse_signed_number(match.group("balance"))

        if rest.startswith("Opening Balance"):
            opening_balance = balance
            has_explicit_opening = True
            continue

        rows.append(
            {
                "date": match.group("date"),
                "description": rest,
                "amount": _parse_signed_number(match.group("amount")),
                "balance": balance,
            }
        )

    flush()
    return sections
