import hashlib
import io
import re
from datetime import datetime

import pdfplumber

from app.parsers.base import ParsedTransaction

DEFAULT_CURRENCY = "KES"

_OPENING_BALANCE_RE = re.compile(r"Opening Balance\s+([\d,]+\.\d{2})")

# The transaction table's real per-row structure is invisible to
# pdfplumber's table detection here - every transaction on a page comes
# back merged into one giant multi-line cell per column, since there
# are no horizontal rules between individual rows (only the outer/column
# borders). Row boundaries are reconstructed from the Date column's own
# word y-positions instead - each date renders exactly once per row.
_MIN_MERGED_ROW_HEIGHT = 40


def _parse_number(value: str) -> float:
    return float(value.replace(",", ""))


def _dedupe_hash(*, description: str, amount: float) -> str:
    # No separate reference column here (unlike M-Pesa/Equity Bank) - the
    # reference code is already embedded in `description`, so hashing on
    # description + amount achieves the same "not reference alone"
    # requirement the runbook calls for on every provider.
    fingerprint = f"{description}|{amount:.2f}"
    return hashlib.sha256(fingerprint.encode()).hexdigest()


def _find_merged_transaction_row(page):
    candidates = []
    for table in page.find_tables():
        for row in table.rows:
            height = row.bbox[3] - row.bbox[1]
            # The real transaction block, not the per-page repeating
            # summary row (which only has its first cell populated,
            # date/description/balance all None) or a single-line row.
            if height >= _MIN_MERGED_ROW_HEIGHT and row.cells[0] is not None and row.cells[-1] is not None:
                candidates.append(row)
    if not candidates:
        return None
    return max(candidates, key=lambda row: row.bbox[3] - row.bbox[1])


def _words_in_bbox(words, bbox, top, bottom):
    x0, x1 = bbox[0], bbox[2]
    return [w for w in words if x0 - 1 <= w["x0"] < x1 + 1 and top <= w["top"] < bottom]


def _parse_page(page) -> list[dict]:
    row = _find_merged_transaction_row(page)
    if row is None:
        return []

    date_bbox = row.cells[0]
    description_bbox = row.cells[1]
    balance_bbox = row.cells[-1]
    top, bottom = row.bbox[1], row.bbox[3]

    words = page.extract_words()
    date_words = sorted(_words_in_bbox(words, date_bbox, top, bottom), key=lambda w: w["top"])
    balance_words = sorted(_words_in_bbox(words, balance_bbox, top, bottom), key=lambda w: w["top"])
    description_words = _words_in_bbox(words, description_bbox, top, bottom)

    # One row anchor per date - a date renders on exactly one line per
    # transaction, unlike the description, which can wrap onto extras.
    row_tops = [w["top"] for w in date_words]
    if not row_tops:
        return []

    row_bands = list(zip(row_tops, row_tops[1:] + [bottom]))

    def band_for(y: float) -> int | None:
        for i, (band_top, band_bottom) in enumerate(row_bands):
            if band_top - 2 <= y < band_bottom - 2:
                return i
        return None

    descriptions: list[list[str]] = [[] for _ in row_bands]
    for w in sorted(description_words, key=lambda w: (w["top"], w["x0"])):
        band = band_for(w["top"])
        if band is not None:
            descriptions[band].append(w["text"])

    balances = [None] * len(row_bands)
    for w in balance_words:
        band = band_for(w["top"])
        if band is not None:
            balances[band] = w["text"]

    rows = []
    for i, date_word in enumerate(date_words):
        if balances[i] is None:
            continue  # a row missing its own balance can't be validated - skip it
        rows.append(
            {
                "date": date_word["text"],
                "description": " ".join(descriptions[i]),
                "balance": _parse_number(balances[i]),
            }
        )
    return rows


def parse_ncba_bank_statement(content: bytes) -> list[ParsedTransaction]:
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        opening_balance_match = _OPENING_BALANCE_RE.search(pdf.pages[0].extract_text() or "")
        raw_rows: list[dict] = []
        for page in pdf.pages:
            raw_rows.extend(_parse_page(page))

    previous_balance = (
        _parse_number(opening_balance_match.group(1))
        if opening_balance_match
        else (raw_rows[0]["balance"] if raw_rows else 0.0)
    )

    transactions: list[ParsedTransaction] = []
    # Already chronological (oldest first) in the source - no reversal needed.
    for row in raw_rows:
        amount = round(abs(row["balance"] - previous_balance), 2)
        direction = "in" if row["balance"] >= previous_balance else "out"
        previous_balance = row["balance"]

        transactions.append(
            {
                "txn_date": datetime.strptime(row["date"], "%d/%m/%Y").date(),
                "amount": amount,
                "currency": DEFAULT_CURRENCY,
                "direction": direction,
                "counterparty": None,  # not mapped for MVP1 - see the runbook
                "description": row["description"],
                "balance_after": row["balance"],
                "dedupe_hash": _dedupe_hash(description=row["description"], amount=amount),
            }
        )

    return transactions
