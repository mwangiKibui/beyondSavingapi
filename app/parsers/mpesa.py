import hashlib
import io
from datetime import datetime
from typing import Literal

import pdfplumber

from app.parsers.base import ParsedTransaction

# M-Pesa operates only in Kenya - every transaction on this statement is
# KES, regardless of the account's own currency setting.
CURRENCY = "KES"

_HEADER_RECEIPT_NO = "Receipt No."
_HEADER_COMPLETION_TIME = "Completion Time"
_HEADER_DETAILS = "Details"
_HEADER_STATUS = "Transaction Status"
_HEADER_PAID_IN = "Paid In"
_HEADER_WITHDRAWN = "Withdrawn"
_HEADER_BALANCE = "Balance"


def _extract_raw_rows(content: bytes) -> list[dict]:
    """Pulls every transaction-table row across all pages, as raw strings.

    pdfplumber's table detection isn't perfectly consistent page to page
    on this document - one page's table has 2 extra empty columns
    splitting "Details" from the rest, the other doesn't - so columns
    are located by the header row's own text rather than a fixed index.
    """
    rows: list[dict] = []

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                if not table or _HEADER_RECEIPT_NO not in table[0]:
                    continue  # not the transaction table (summary, etc.)

                header = table[0]
                col = {name: idx for idx, name in enumerate(header) if name}

                for raw_row in table[1:]:
                    receipt_no = raw_row[col[_HEADER_RECEIPT_NO]]
                    completion_time = raw_row[col[_HEADER_COMPLETION_TIME]]
                    if receipt_no is None or completion_time is None:
                        # A wrapped counterparty name at the end of the row
                        # above sometimes spills into a phantom extra row
                        # with no receipt/time of its own (occasionally
                        # even truncated by a character). That text is
                        # already part of the real row's multi-line
                        # Details cell above, so this adds nothing real -
                        # skip rather than risk appending a garbled dupe.
                        continue

                    rows.append(
                        {
                            "receipt_no": receipt_no,
                            "completion_time": completion_time,
                            "details": raw_row[col[_HEADER_DETAILS]] or "",
                            "status": raw_row[col[_HEADER_STATUS]],
                            "paid_in": raw_row[col[_HEADER_PAID_IN]],
                            "withdrawn": raw_row[col[_HEADER_WITHDRAWN]],
                            "balance": raw_row[col[_HEADER_BALANCE]],
                        }
                    )

    return rows


def _clean_details(raw: str) -> str:
    # Multi-line cells sometimes end with a bare page number that bled
    # into the table's bounding box - strip lines that are only digits,
    # then flatten to a single line (still the verbatim words, just
    # without the mid-sentence newlines a wrapped cell introduces).
    lines = [line.strip() for line in raw.split("\n")]
    lines = [line for line in lines if line and not line.isdigit()]
    return " ".join(lines)


def _parse_number(value: str) -> float:
    return float(value.replace(",", ""))


def _amount_and_direction(paid_in: str | None, withdrawn: str | None) -> tuple[float, Literal["in", "out"]]:
    # Exactly one of the two is ever populated (confirmed against the
    # real sample) - Paid In is always non-negative, Withdrawn always
    # carries its own leading "-".
    if paid_in:
        return _parse_number(paid_in), "in"
    return abs(_parse_number(withdrawn or "0")), "out"


def _dedupe_hash(*, receipt_no: str, details: str, amount: float) -> str:
    # Receipt No. alone isn't unique per row - Fuliza (overdraft)
    # transactions split into 2-3 rows sharing one receipt. Combining
    # with the cleaned details text and amount distinguishes them.
    fingerprint = f"{receipt_no}|{details}|{amount:.2f}"
    return hashlib.sha256(fingerprint.encode()).hexdigest()


def parse_mpesa_statement(content: bytes) -> list[ParsedTransaction]:
    parsed: list[tuple[datetime, ParsedTransaction]] = []

    for raw_row in _extract_raw_rows(content):
        status = (raw_row["status"] or "").strip()
        if status and status.lower() != "completed":
            # No real sample has shown a non-Completed row yet - excluded
            # defensively rather than posted as a real transaction, per
            # docs/provider-onboarding-runbook.md's M-Pesa field mapping.
            continue

        completion_time = datetime.strptime(raw_row["completion_time"], "%Y-%m-%d %H:%M:%S")
        details = _clean_details(raw_row["details"])
        amount, direction = _amount_and_direction(raw_row["paid_in"], raw_row["withdrawn"])
        balance_raw = (raw_row["balance"] or "").strip()

        transaction: ParsedTransaction = {
            "txn_date": completion_time.date(),
            "amount": amount,
            "currency": CURRENCY,
            "direction": direction,
            "counterparty": None,  # not mapped for M-Pesa - see the runbook
            "description": details,
            "balance_after": _parse_number(balance_raw) if balance_raw else None,
            "dedupe_hash": _dedupe_hash(
                receipt_no=raw_row["receipt_no"], details=details, amount=amount
            ),
        }
        parsed.append((completion_time, transaction))

    # The statement lists newest-first - sort ascending (stable, so
    # same-timestamp rows like a Fuliza pair keep their original relative
    # order) to satisfy the oldest-first contract the worker relies on.
    parsed.sort(key=lambda item: item[0])
    return [transaction for _, transaction in parsed]
