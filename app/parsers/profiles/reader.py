"""ab-312: the shared statement reader - runs any profile over a PDF.

Per page: find the table's heading band (every column heading, in order),
work out the column bounds (the PDF's ruling lines, midway between
headings, or the profile's explicit fractions), then turn each line below
it into cells. A line whose date cell parses starts a row; a line without
a date continues the row above (wrapped descriptions). Opening balance and
totals rows are taken out and kept for the balance check. Rows come back
oldest first, in exactly the shape the bespoke parsers return.

read_statement() reports the balance check (the mapping wizard's preview
needs the failing rows); read_transactions() is parser-shaped and raises
when the check fails.
"""

import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import pdfplumber

from app.parsers.base import (
    BalanceCheck,
    ParsedTransaction,
    RunningBalanceMismatch,
    StatementParseError,
    check_balances,
)
from app.parsers.profiles.layout import Line, page_lines
from app.parsers.profiles.schema import StatementProfile
from app.parsers.profiles.text import normalise

_PAGE_NUMBER = re.compile(r"^(page\s*)?\d+\s*(of|/)\s*\d+$|^\d{1,3}$", re.IGNORECASE)
_RULE_MERGE = 2.0


@dataclass
class ReadResult:
    transactions: list[ParsedTransaction]
    opening_balance: float | None
    totals: tuple[float, float] | None
    check: BalanceCheck


@dataclass
class _Row:
    cells: dict[str, str]
    continuation: list[str]


def _amount(text: str, profile: StatementProfile) -> float | None:
    s = text.strip().replace(" ", "") if profile.amounts.thousands_sep != " " else text.strip()
    if not s:
        return None
    negative = False
    upper = s.upper()
    if upper.endswith("DR"):
        negative, s = True, s[:-2]
    elif upper.endswith("CR"):
        s = s[:-2]
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    if s.startswith("-"):
        negative, s = True, s[1:]
    sep = profile.amounts.thousands_sep
    if sep:
        s = s.replace(sep, "")
    s = s.replace(" ", "")
    if profile.amounts.decimal_mark == ",":
        s = s.replace(",", ".")
    try:
        value = Decimal(s)
    except InvalidOperation:
        raise StatementParseError(f"Couldn't read the amount '{text.strip()}'") from None
    value = -value if negative else value
    return round(float(value), 2)


def _date(text: str, profile: StatementProfile) -> date | None:
    words = text.split()
    needed = len(profile.date_format.split())
    if len(words) < needed:
        return None
    try:
        return datetime.strptime(" ".join(words[:needed]), profile.date_format).date()
    except ValueError:
        return None


def _header_band(lines: list[Line], profile: StatementProfile, start: int = 0):
    """(first line index, last line index, {column index: (x0, x1)}) of the
    heading band, or None. Every column with a header must be found, left to
    right, within header_lines consecutive lines."""
    wanted = [(i, normalise(c.header).split()) for i, c in enumerate(profile.table.columns) if c.header]
    if not wanted:
        return None
    span = profile.table.header_lines
    for top in range(start, len(lines)):
        band = lines[top : top + span]
        words = sorted((w for line in band for w in line.words), key=lambda w: w.x0)
        tokens = [normalise(w.text) for w in words]
        found, cursor = {}, -1
        for col, parts in wanted:
            hit = None
            for j, token in enumerate(tokens):
                if j > cursor and token == parts[0]:
                    group = [words[j]]
                    ok = True
                    for part in parts[1:]:
                        match = next(
                            (w for k, w in enumerate(words) if normalise(w.text) == part and w not in group
                             and abs(w.x0 - group[0].x0) < 160),
                            None,
                        )
                        if match is None:
                            ok = False
                            break
                        group.append(match)
                    if ok:
                        hit = (j, min(w.x0 for w in group), max(w.x1 for w in group))
                        break
            if hit is None:
                break
            cursor = hit[0]
            found[col] = (hit[1], hit[2])
        if len(found) == len(wanted):
            last = top + span - 1
            # A single-line profile: don't swallow the first row into the band.
            if span == 1:
                last = top
            return top, min(last, len(lines) - 1), found
    return None


def _bounds(page, profile: StatementProfile, header: dict[int, tuple[float, float]], band_top: float, band_bottom: float):
    width = float(page.width)
    cols = profile.table.columns
    mode = profile.table.column_bounds
    if mode == "explicit":
        return {i: (c.x0 * width, c.x1 * width) for i, c in enumerate(cols)}
    if mode == "header_gaps":
        located = sorted(header.items(), key=lambda kv: kv[1][0])
        out = {}
        for n, (col, (x0, x1)) in enumerate(located):
            left = 0.0 if n == 0 else out[located[n - 1][0]][1]
            right = width if n == len(located) - 1 else (x1 + located[n + 1][1][0]) / 2
            out[col] = (left, right)
        return out
    # ruled: vertical lines crossing the heading band, merged when close.
    xs = sorted(
        e["x0"]
        for e in page.edges
        if e.get("orientation") == "v" and e["top"] <= band_bottom + 2 and e["bottom"] >= band_top - 2
    )
    merged: list[float] = []
    for x in xs:
        if not merged or x - merged[-1] > _RULE_MERGE:
            merged.append(x)
    slots = list(zip(merged, merged[1:], strict=False))
    out = {}
    for col, (x0, x1) in header.items():
        centre = (x0 + x1) / 2
        slot = next((s for s in slots if s[0] <= centre <= s[1]), None)
        if slot is None:
            raise StatementParseError("The statement's columns don't match this layout")
        out[col] = slot
    if len(set(out.values())) != len(out):
        raise StatementParseError("The statement's columns don't match this layout")
    return out


def _cells(line: Line, bounds: dict[int, tuple[float, float]], profile: StatementProfile) -> dict[str, str]:
    by_col: dict[int, list[str]] = {i: [] for i in bounds}
    for word in line.words:
        centre = (word.x0 + word.x1) / 2
        col = next((i for i, (x0, x1) in bounds.items() if x0 <= centre <= x1), None)
        if col is None:
            col = min(bounds, key=lambda i: min(abs(centre - bounds[i][0]), abs(centre - bounds[i][1])))
        by_col[col].append(word.text)
    cells: dict[str, str] = {}
    for i, texts in by_col.items():
        role = profile.table.columns[i].role
        if role != "ignore" and texts:
            cells[role] = " ".join(texts)
    return cells


def _starts(text: str, label: str | None) -> bool:
    return bool(label) and normalise(text).startswith(normalise(label))


def _skipped(text: str, profile: StatementProfile) -> bool:
    if profile.table.skip_page_numbers and _PAGE_NUMBER.match(text.strip()):
        return True
    norm = normalise(text)
    for rule in profile.table.skip_rows:
        target = normalise(rule.text)
        if (
            (rule.match == "equals" and norm == target)
            or (rule.match == "starts_with" and norm.startswith(target))
            or (rule.match == "contains" and target in norm)
        ):
            return True
    return False


def _hash(row: dict, reference: str | None) -> str:
    if reference:
        key = f"{reference}|{row['description']}|{row['amount']:.2f}"
    else:
        key = (
            f"{row['txn_date'].isoformat()}|{row['amount']:.2f}|{row['direction']}|"
            f"{'' if row['balance_after'] is None else f'{row['balance_after']:.2f}'}|{row['description']}"
        )
    return hashlib.sha256(key.encode()).hexdigest()


def _to_transaction(row: _Row, profile: StatementProfile, currency: str) -> ParsedTransaction | None:
    cells = row.cells
    txn_date = _date(cells.get("date", ""), profile)
    description = " ".join([cells.get("description", ""), *row.continuation]).strip() or None
    if "signed_amount" in {c.role for c in profile.table.columns}:
        value = _amount(cells.get("signed_amount", ""), profile)
        if not value:
            return None
        positive_in = profile.signed_amount_positive_is == "in"
        direction = ("in" if positive_in else "out") if value > 0 else ("out" if positive_in else "in")
        amount = abs(value)
    else:
        money_in = _amount(cells.get("money_in", ""), profile)
        money_out = _amount(cells.get("money_out", ""), profile)
        if money_in and money_out:
            raise StatementParseError(f"A row on {txn_date} has both money in and money out")
        if money_in:
            direction, amount = "in", abs(money_in)
        elif money_out:
            direction, amount = "out", abs(money_out)
        else:
            return None
    balance = _amount(cells.get("balance", ""), profile)
    txn: dict = {
        "txn_date": txn_date,
        "amount": amount,
        "currency": profile.currency or currency,
        "direction": direction,
        "counterparty": None,
        "description": description,
        "balance_after": balance,
    }
    txn["dedupe_hash"] = _hash(txn, (cells.get("reference") or "").strip() or None)
    return txn  # type: ignore[return-value]


def read_statement(content: bytes, profile: StatementProfile, *, default_currency: str) -> ReadResult:
    rows: list[_Row] = []
    opening: float | None = None
    totals: tuple[float, float] | None = None
    bounds = None
    ended = False
    try:
        pdf = pdfplumber.open(io.BytesIO(content))
    except Exception:
        raise StatementParseError("This file couldn't be opened as a PDF") from None
    with pdf:
        for page_no, page in enumerate(pdf.pages):
            if ended:
                break
            lines = page_lines(page, y_tolerance=profile.table.y_tolerance)
            band = _header_band(lines, profile)
            if band is not None:
                top, last, header = band
                bounds = _bounds(page, profile, header, lines[top].top - 2, lines[last].top + 12)
                start = last + 1
            elif profile.table.column_bounds == "explicit" or (
                page_no > 0 and not profile.table.header_repeats_each_page and bounds is not None
            ):
                if bounds is None:
                    width = float(page.width)
                    bounds = {i: (c.x0 * width, c.x1 * width) for i, c in enumerate(profile.table.columns)}
                start = 0
            elif page_no == 0:
                raise StatementParseError("The statement's table headings weren't found for this layout")
            else:
                continue  # a page without the table (notes, disclaimers)
            current: _Row | None = None
            for line in lines[start:]:
                text = line.text
                if any(_starts(text, m) for m in profile.table.end_markers):
                    ended = True
                    break
                if _skipped(text, profile):
                    continue
                cells = _cells(line, bounds, profile)
                desc = cells.get("description", text)
                if profile.opening_balance.source == "table_row" and _starts(desc, profile.opening_balance.label):
                    opening = _amount(cells.get("balance", ""), profile)
                    current = None
                    continue
                if profile.totals.source == "table_row" and _starts(desc, profile.totals.label):
                    total_in = _amount(cells.get("money_in", ""), profile) or 0.0
                    total_out = _amount(cells.get("money_out", ""), profile) or 0.0
                    totals = (abs(total_in), abs(total_out))
                    current = None
                    continue
                if _date(cells.get("date", ""), profile) is not None:
                    current = _Row(cells=cells, continuation=[])
                    rows.append(current)
                elif current is not None and cells.get("description"):
                    current.continuation.append(cells["description"])

    transactions = [t for t in (_to_transaction(r, profile, default_currency) for r in rows) if t is not None]
    if profile.table.row_order == "newest_first":
        transactions.reverse()
    if opening is None and profile.opening_balance.source == "first_row" and transactions:
        first = transactions[0]
        if first["balance_after"] is not None:
            delta = first["amount"] if first["direction"] == "in" else -first["amount"]
            opening = round(first["balance_after"] - delta, 2)
    check = check_balances(transactions, opening_balance=opening, totals=totals)
    return ReadResult(transactions=transactions, opening_balance=opening, totals=totals, check=check)


def read_transactions(content: bytes, profile: StatementProfile, *, default_currency: str) -> list[ParsedTransaction]:
    """Parser-shaped: the rows, or StatementParseError when they can't be
    proven by the balance check."""
    result = read_statement(content, profile, default_currency=default_currency)
    if not result.check.passed:
        if result.check.kind == "running" and result.check.failing_rows:
            raise RunningBalanceMismatch(result.check.message)
        raise StatementParseError(result.check.message or "The statement's balances don't add up")
    return result.transactions
