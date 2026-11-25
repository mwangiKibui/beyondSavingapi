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
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import pdfplumber

from app.parsers.base import (
    UNLISTED_CHARGE,
    BalanceCheck,
    ParsedTransaction,
    RunningBalanceMismatch,
    StatementParseError,
    check_balances,
    resolve_leading_tie,
    totals_failures,
)
from app.parsers.profiles.layout import MONEY_TOKEN, Line, page_lines
from app.parsers.profiles.schema import StatementProfile
from app.parsers.profiles.tabular import cell_text, find_header_row, load_grid
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
    # (y, text) of the description lines that belong to this row besides
    # its own line at `y` - ordered by y when the description is built.
    continuation: list[tuple[float, str]]
    y: float = 0.0


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


def _datetime(text: str, profile: StatementProfile) -> datetime | None:
    words = text.split()
    needed = len(profile.date_format.split())
    if len(words) < needed:
        return None
    try:
        return datetime.strptime(" ".join(words[:needed]), profile.date_format)
    except ValueError:
        return None


def _date(text: str, profile: StatementProfile) -> date | None:
    parsed = _datetime(text, profile)
    return parsed.date() if parsed else None


def _summary_totals(lines: list[Line], profile: StatementProfile) -> tuple[float, float] | None:
    """A line starting with the totals label and holding exactly two
    amounts: money in, then money out (ab-313, M-Pesa's SUMMARY)."""
    for line in lines:
        if not _starts(line.text, profile.totals.label):
            continue
        amounts = [w.text for w in line.words if MONEY_TOKEN.match(w.text)]
        if len(amounts) == 2:
            return abs(_amount(amounts[0], profile) or 0.0), abs(_amount(amounts[1], profile) or 0.0)
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
        found, cursor, used = {}, -1, set()
        for col, parts in wanted:
            hit = None
            for j, token in enumerate(tokens):
                if j > cursor and token == parts[0] and j not in used:
                    group = [j]
                    ok = True
                    for part in parts[1:]:
                        # The heading's next word: unused, not left of the
                        # word before it (a stacked heading may start a little
                        # left), and the nearest such - so "Value Date" takes
                        # its own "Date", not the Date column's (ab-314).
                        prev = words[group[-1]]
                        options = [
                            k for k, w in enumerate(words)
                            if tokens[k] == part and k not in group and k not in used
                            and w.x0 >= prev.x0 - 40 and abs(w.x0 - words[group[0]].x0) < 160
                        ]
                        if not options:
                            ok = False
                            break
                        group.append(min(options, key=lambda k: abs(words[k].x0 - prev.x1) + abs(words[k].top - prev.top)))
                    if ok:
                        chosen = [words[k] for k in group]
                        hit = (j, min(w.x0 for w in chosen), max(w.x1 for w in chosen))
                        used.update(group)
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


def _ruled_bottom(page, band_top: float, band_bottom: float) -> float | None:
    """Where the ruled table ends. Rules are often drawn one segment per
    row, so follow the leftmost rule crossing the heading band down while
    its segments touch; the table ends where that chain stops."""
    crossing = [
        e for e in page.edges
        if e.get("orientation") == "v" and e["top"] <= band_bottom + 2 and e["bottom"] >= band_top - 2
    ]
    if not crossing:
        return None
    x = min(e["x0"] for e in crossing)
    segments = sorted(
        (e["top"], e["bottom"]) for e in page.edges if e.get("orientation") == "v" and abs(e["x0"] - x) <= _RULE_MERGE
    )
    bottom = max(e["bottom"] for e in crossing if abs(e["x0"] - x) <= _RULE_MERGE)
    for top, seg_bottom in segments:
        if top <= bottom + _RULE_MERGE and seg_bottom > bottom:
            bottom = seg_bottom
    # Only the heading is ruled (Equity, ab-320): the body has no rules to
    # end at - the end markers / totals row end it instead.
    if bottom <= band_bottom:
        return None
    return bottom


def _cells(line: Line, bounds: dict[int, tuple[float, float]], profile: StatementProfile) -> dict[str, str]:
    by_col: dict[int, list[str]] = {i: [] for i in bounds}
    words = line.words
    if profile.table.word_order == "top_then_left":
        words = sorted(words, key=lambda w: (w.top, w.x0))
    for word in words:
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


def _status_kept(cells: dict[str, str], profile: StatementProfile) -> bool:
    keep = profile.table.status_keep
    status = cells.get("status", "").strip()
    return not (keep and status and normalise(status) != normalise(keep))


def _hash(row: dict, reference: str | None) -> str:
    if reference:
        key = f"{reference}|{row['description']}|{row['amount']:.2f}"
    else:
        key = (
            f"{row['txn_date'].isoformat()}|{row['amount']:.2f}|{row['direction']}|"
            f"{'' if row['balance_after'] is None else f'{row['balance_after']:.2f}'}|{row['description']}"
        )
    return hashlib.sha256(key.encode()).hexdigest()


def _to_transaction(
    row: _Row, profile: StatementProfile, currency: str, forced: tuple[float, str] | None = None
) -> ParsedTransaction | None:
    cells = row.cells
    txn_date = _date(cells.get("date", ""), profile)
    pieces = [text for _, text in sorted([(row.y, cells.get("description", "")), *row.continuation], key=lambda p: p[0])]
    if profile.table.drop_digit_only_lines:
        pieces = [piece for piece in pieces if not piece.strip().isdigit()]
    description = " ".join(p.strip() for p in pieces if p.strip()) or None
    if forced is not None:
        amount, direction = forced  # amounts_from: balance_change
    elif "signed_amount" in {c.role for c in profile.table.columns}:
        value = _amount(cells.get("signed_amount", ""), profile)
        if value is None:
            return None
        positive_in = profile.signed_amount_positive_is == "in"
        direction = ("in" if positive_in else "out") if value > 0 else ("out" if positive_in else "in")
        amount = abs(value)
    else:
        money_in = _amount(cells.get("money_in", ""), profile)
        money_out = _amount(cells.get("money_out", ""), profile)
        if money_in and money_out:
            raise StatementParseError(f"A row on {txn_date} has both money in and money out")
        # A filled cell counts even at 0.00 (M-Pesa's "M-Shwari Loan
        # Request 0.00" rows, ab-313); a non-zero cell wins over a zero one.
        if money_in is not None and (money_in or not money_out):
            direction, amount = "in", abs(money_in)
        elif money_out is not None:
            direction, amount = "out", abs(money_out)
        else:
            return None
    balance = _amount(cells.get("balance", ""), profile)
    txn: dict = {
        "txn_date": txn_date,
        "amount": amount,
        "currency": currency,
        "direction": direction,
        "counterparty": None,
        "description": description,
        "balance_after": balance,
    }
    if profile.dedupe == "description_amount":
        txn["dedupe_hash"] = hashlib.sha256(f"{description}|{amount:.2f}".encode()).hexdigest()
    else:
        txn["dedupe_hash"] = _hash(txn, (cells.get("reference") or "").strip() or None)
    return txn  # type: ignore[return-value]


@dataclass
class _Collected:
    """Rows and the statement's own anchors, gathered from any file type."""

    rows: list[_Row] = field(default_factory=list)
    opening: float | None = None
    totals: tuple[float, float] | None = None
    # The totals row's balance cell (opening_balance: from_totals).
    closing: float | None = None
    currency: str | None = None
    current: _Row | None = None
    # continuation "nearest": lines waiting for the next row to decide.
    pending: list[tuple[float, str]] = field(default_factory=list)
    ended: bool = False


def _flush_pending(state: _Collected, new_row: _Row | None) -> None:
    """Gives each waiting line to the nearer of the row above it on this
    page and `new_row` (the earlier one on a tie)."""
    for y, text in state.pending:
        above, below = state.current, new_row
        if above is not None and (below is None or abs(y - above.y) <= abs(y - below.y)):
            above.continuation.append((y, text))
        elif below is not None:
            below.continuation.append((y, text))
    state.pending = []


def _take_line(
    state: _Collected, text: str, cells: dict[str, str], profile: StatementProfile, y: float = 0.0
) -> None:
    """One line of the table (a PDF line or a spreadsheet row): ends the
    table, is skipped, is an opening/totals row, starts a transaction, or
    continues a row's description."""
    if any(_starts(text, m) for m in profile.table.end_markers):
        _end_page(state)
        state.ended = True
        return
    if _skipped(text, profile):
        return
    desc = cells.get("description", text)
    if profile.opening_balance.source == "table_row" and _starts(desc, profile.opening_balance.label):
        state.opening = _amount(cells.get("balance", ""), profile)
        _end_page(state)
        return
    if profile.totals.source == "table_row" and _starts(desc, profile.totals.label):
        total_in = _amount(cells.get("money_in", ""), profile) or 0.0
        total_out = _amount(cells.get("money_out", ""), profile) or 0.0
        state.totals = (abs(total_in), abs(total_out))
        state.closing = _amount(cells.get("balance", ""), profile)
        _end_page(state)
        return
    if _date(cells.get("date", ""), profile) is not None:
        row = _Row(cells=cells, continuation=[], y=y)
        _flush_pending(state, row)
        state.current = row
        if _status_kept(cells, profile):
            state.rows.append(row)
    elif cells.get("description"):
        if profile.table.continuation == "nearest":
            state.pending.append((y, cells["description"]))
        elif state.current is not None:
            state.current.continuation.append((y, cells["description"]))


def _end_page(state: _Collected) -> None:
    """Waiting lines go to the last row; nothing continues past here."""
    _flush_pending(state, None)
    state.current = None


def _labelled(lines: list[Line], label: str | None, pattern: re.Pattern) -> str | None:
    """The first word matching `pattern` after `label` on a line."""
    if not label:
        return None
    wanted = normalise(label).split()
    for line in lines:
        tokens = [normalise(w.text) for w in line.words]
        for i in range(len(tokens) - len(wanted) + 1):
            if tokens[i : i + len(wanted)] == wanted:
                after = next((w.text for w in line.words[i + len(wanted) :] if pattern.match(w.text)), None)
                if after is not None:
                    return after
    return None


_CURRENCY = re.compile(r"^[A-Z]{3}$")


def _above_table(state: _Collected, lines: list[Line], profile: StatementProfile) -> None:
    """Anchors printed above the table, taken from the first page that has one."""
    if profile.totals.source == "summary_row" and state.totals is None:
        state.totals = _summary_totals(lines, profile)
    if profile.totals.source == "labelled_lines" and state.totals is None:
        money_in = _labelled(lines, profile.totals.label, MONEY_TOKEN)
        money_out = _labelled(lines, profile.totals.out_label, MONEY_TOKEN)
        if money_in is not None and money_out is not None:
            state.totals = (abs(_amount(money_in, profile)), abs(_amount(money_out, profile)))
    if profile.opening_balance.source == "labelled_line" and state.opening is None:
        opening = _labelled(lines, profile.opening_balance.label, MONEY_TOKEN)
        if opening is not None:
            state.opening = _amount(opening, profile)
    if profile.currency_label and state.currency is None:
        state.currency = _labelled(lines, profile.currency_label, _CURRENCY)


def _collect_pdf(content: bytes, profile: StatementProfile) -> _Collected:
    state = _Collected()
    bounds = None
    try:
        pdf = pdfplumber.open(io.BytesIO(content))
    except Exception:
        raise StatementParseError("This file couldn't be opened as a PDF") from None
    with pdf:
        for page_no, page in enumerate(pdf.pages):
            if state.ended:
                break
            lines = page_lines(page, y_tolerance=profile.table.y_tolerance)
            band = _header_band(lines, profile)
            table_bottom = None
            if band is not None:
                top, last, header = band
                bounds = _bounds(page, profile, header, lines[top].top - 2, lines[last].top + 12)
                start = last + 1
                if profile.table.column_bounds == "ruled":
                    table_bottom = _ruled_bottom(page, lines[top].top - 2, lines[last].top + 12)
                _above_table(state, lines[:top], profile)
            elif (profile.table.column_bounds == "explicit" and not any(c.header for c in profile.table.columns)) or (
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
            state.current = None
            for line in lines[start:]:
                if table_bottom is not None and line.top > table_bottom:
                    break  # below the ruled table: footers, disclaimers
                _take_line(state, line.text, _cells(line, bounds, profile), profile, line.top)
                if state.ended:
                    break
            _end_page(state)
            # With a heading on every page, an end marker (a per-page
            # footer such as NCBA's notice) ends only this page's table.
            if state.ended and profile.table.header_repeats_each_page:
                state.ended = False
    return state


def _collect_spreadsheet(content: bytes, profile: StatementProfile) -> _Collected:
    """ab-319: CSV / XLSX. The heading row is the first whose cells hold
    every column heading; each row below is one line of the table."""
    state = _Collected()
    grid = load_grid(content, profile.file_type, sheet=profile.table.sheet)
    found = find_header_row(grid, profile)
    if found is None:
        raise StatementParseError("The statement's table headings weren't found for this layout")
    header_at, mapping = found
    if profile.totals.source == "summary_row":
        state.totals = _spreadsheet_summary(grid[:header_at], profile)
    for index, row in enumerate(grid[header_at + 1 :], start=header_at + 1):
        texts = [cell_text(v, profile, None) for v in row]
        text = " ".join(t for t in texts if t)
        if not text:
            continue
        cells: dict[str, str] = {}
        for col, index in mapping.items():
            role = profile.table.columns[col].role
            value = cell_text(row[index] if index < len(row) else None, profile, role)
            if role != "ignore" and value:
                cells[role] = value
        _take_line(state, text, cells, profile, float(index))
        if state.ended:
            break
    _end_page(state)
    return state


def _spreadsheet_summary(rows: list[list], profile: StatementProfile) -> tuple[float, float] | None:
    for row in rows:
        texts = [cell_text(v, profile, "balance") for v in row]
        filled = [t for t in texts if t]
        if not filled or not _starts(filled[0], profile.totals.label):
            continue
        amounts = []
        for t in filled[1:]:
            try:
                amounts.append(_amount(t, profile))
            except StatementParseError:
                continue
        if len(amounts) >= 2 and None not in amounts[:2]:
            return abs(amounts[0]), abs(amounts[1])
    return None


def read_statement(content: bytes, profile: StatementProfile, *, default_currency: str) -> ReadResult:
    if profile.file_type == "pdf":
        state = _collect_pdf(content, profile)
    else:
        state = _collect_spreadsheet(content, profile)
    rows, opening, totals = state.rows, state.opening, state.totals
    currency = state.currency or profile.currency or default_currency
    balance_change = profile.amounts_from == "balance_change"
    if balance_change:
        # A row without a balance can't give an amount - skipped, as the
        # bespoke parsers did.
        rows = [r for r in rows if _amount(r.cells.get("balance", ""), profile) is not None]

    keyed_rows = [(_datetime(r.cells.get("date", ""), profile), r) for r in rows]
    if profile.table.row_order == "newest_first":
        if profile.table.tie_order == "reverse":
            keyed_rows.reverse()
        # Stable: rows with the same date/time keep the order above.
        keyed_rows.sort(key=lambda kr: kr[0])

    if opening is None and profile.opening_balance.source == "from_totals" and totals and state.closing is not None:
        opening = round(state.closing - totals[0] + totals[1], 2)

    if balance_change:
        if opening is None:
            check = BalanceCheck(kind="totals", passed=False, message="The statement's opening balance wasn't found")
            return ReadResult(transactions=[], opening_balance=None, totals=totals, check=check)
        keyed, previous = [], opening
        for when, row in keyed_rows:
            balance = _amount(row.cells["balance"], profile)
            forced = (round(abs(balance - previous), 2), "in" if balance >= previous else "out")
            previous = balance
            keyed.append((when, _to_transaction(row, profile, currency, forced)))
    else:
        keyed = [
            (when, t) for when, row in keyed_rows if (t := _to_transaction(row, profile, currency)) is not None
        ]
    if profile.table.resolve_first_tie:
        keyed = resolve_leading_tie(keyed)
    transactions = [t for _, t in keyed]
    if opening is None and profile.opening_balance.source == "first_row" and transactions:
        first = transactions[0]
        if first["balance_after"] is not None:
            delta = first["amount"] if first["direction"] == "in" else -first["amount"]
            opening = round(first["balance_after"] - delta, 2)
    added = 0
    if profile.unlisted_charges and opening is not None:
        transactions, added = _with_unlisted_charges(transactions, opening)
    check = check_balances(transactions, opening_balance=opening, totals=totals, mode=profile.balance_check)
    if added and check.passed:
        check = _charges_proved(transactions, totals)
    return ReadResult(transactions=transactions, opening_balance=opening, totals=totals, check=check)


def _with_unlisted_charges(transactions: list[ParsedTransaction], opening: float) -> tuple[list, int]:
    """ab-325: after each money-out row whose balance fell by more than its
    amount, a charge row for the difference. Only a fee smaller than the
    payment counts; any other gap is left for the balance check to fail."""
    out, previous, added = [], opening, 0
    for txn in transactions:
        balance = txn["balance_after"]
        sign = 1 if txn["direction"] == "in" else -1
        expected = round(previous + sign * txn["amount"], 2)
        fee = round(expected - balance, 2) if balance is not None else 0.0
        if txn["direction"] == "out" and 0.01 < fee < txn["amount"]:
            out.append({**txn, "balance_after": expected})
            out.append(
                {
                    "txn_date": txn["txn_date"],
                    "amount": fee,
                    "currency": txn["currency"],
                    "direction": "out",
                    "counterparty": None,
                    "description": UNLISTED_CHARGE,
                    "balance_after": balance,
                    "dedupe_hash": hashlib.sha256(f"{txn['dedupe_hash']}|charge".encode()).hexdigest(),
                }
            )
            added += 1
        else:
            out.append(txn)
        previous = balance if balance is not None else expected
    return out, added


def _charges_proved(transactions: list[ParsedTransaction], totals: tuple[float, float] | None) -> BalanceCheck:
    """Added charges stand only if the statement's own totals include them."""
    if totals is None:
        return BalanceCheck(
            kind="totals", passed=False, message="The statement's totals weren't found, so its charges can't be checked"
        )
    if totals_failures(transactions, total_in=totals[0], total_out=totals[1]):
        return BalanceCheck(
            kind="totals",
            passed=False,
            message="The charges taken from the balance don't match the statement's total money out",
        )
    return BalanceCheck(kind="running", passed=True)


def read_transactions(content: bytes, profile: StatementProfile, *, default_currency: str) -> list[ParsedTransaction]:
    """Parser-shaped: the rows, or StatementParseError when they can't be
    proven by the balance check."""
    result = read_statement(content, profile, default_currency=default_currency)
    if not result.check.passed:
        if result.check.kind == "running" and result.check.failing_rows:
            raise RunningBalanceMismatch(result.check.message)
        raise StatementParseError(result.check.message or "The statement's balances don't add up")
    return result.transactions
