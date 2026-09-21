import hashlib
import io
from datetime import datetime
from itertools import permutations
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


def _self_consistent_order(
    cluster: list[ParsedTransaction],
) -> list[ParsedTransaction] | None:
    """Finds the one ordering of a same-timestamp cluster whose balance
    chain is internally self-consistent, when the cluster sits at the
    very start of the statement (no earlier row's balance to anchor to).

    Since there's no known prior balance, each candidate order's own
    first row is used to imply one (solving balance_after = implied +/-
    amount) - trivially satisfiable by any order on its own, so it adds
    no real signal for row 1. Every row after that must actually
    reconcile against the *previous row in that same candidate order*,
    which most permutations fail (confirmed against the real M-Pesa
    fixture: 5 of the 6 orderings of its one genuine 3-row tie fail this
    check, leaving exactly one - Disburse, Request, Repayment - matching
    the hand-verified answer in ab-42's ticket).

    Returns None (leave the original order alone) unless exactly one
    ordering passes - either 0 or >1 valid orderings means this
    technique can't distinguish the real order from noise here.
    """
    valid_orders = []
    for candidate in permutations(cluster):
        first = candidate[0]
        current = round(
            first["balance_after"] - first["amount"]
            if first["direction"] == "in"
            else first["balance_after"] + first["amount"],
            2,
        )
        consistent = True
        for txn in candidate:
            expected = round(
                current + txn["amount"] if txn["direction"] == "in" else current - txn["amount"], 2
            )
            if txn["balance_after"] is None or abs(expected - txn["balance_after"]) > 0.01:
                consistent = False
                break
            current = txn["balance_after"]
        if consistent:
            valid_orders.append(list(candidate))

    return valid_orders[0] if len(valid_orders) == 1 else None


def _resolve_leading_tie(
    parsed: list[tuple[datetime, ParsedTransaction]],
) -> list[tuple[datetime, ParsedTransaction]]:
    """Applies `_self_consistent_order` to the statement's very first
    same-timestamp (full date+time, not just date) cluster only - see
    that function's docstring for why the technique isn't trustworthy
    for a cluster anywhere else in the file, since M-Pesa's Balance
    column isn't one continuous ledger around Fuliza-related entries,
    so an apparent match against some earlier row's balance is as
    likely coincidental as real.
    """
    if len(parsed) < 2:
        return parsed

    first_time = parsed[0][0]
    cluster_end = 1
    while cluster_end < len(parsed) and parsed[cluster_end][0] == first_time:
        cluster_end += 1
    if cluster_end < 2:
        return parsed

    resolved = _self_consistent_order([txn for _, txn in parsed[:cluster_end]])
    if resolved is None:
        return parsed
    return [(first_time, txn) for txn in resolved] + parsed[cluster_end:]


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
    parsed = _resolve_leading_tie(parsed)
    return [transaction for _, transaction in parsed]
