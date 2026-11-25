"""ab-315: a first guess at an unknown statement's layout, for the mapping
wizard to start from (the AI proposal replaces it with ab-314), and the
page snapshot the wizard draws.

The guess is made from the heading words alone ("Date", "Paid In",
"Balance"...), the same heading the fingerprint finds. It is only a
starting point: nothing is saved until the uploader confirms a mapping
that passes the balance check.
"""

import io
from datetime import datetime

import pdfplumber

from app.parsers.profiles.fingerprint import _date_like, _header_clusters, _header_index, _money_like
from app.parsers.profiles.layout import DATE_TOKEN, MONEY_TOKEN, Line, is_body_line, page_lines
from app.parsers.profiles.reader import read_statement
from app.parsers.profiles.schema import InvalidStatementProfile, validate_profile
from app.parsers.profiles.tabular import load_grid
from app.parsers.profiles.text import normalise

# Tried on the date column - day first, as Kenyan statements are. Formats
# with more parts go first, so "02-09-26 12:17 PM" isn't taken as a bare
# date with its time left over.
_FORMATS = [
    "%d/%m/%Y",
    "%d/%m/%y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d-%m-%y",
    "%d.%m.%Y",
    "%d %b %Y",
    "%d-%b-%Y",
    "%d-%b-%y",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d-%m-%Y %H:%M:%S",
    # ab-325: 12-hour times (Airtel Money's "02-09-26 12:17 PM").
    "%d-%m-%y %I:%M %p",
    "%d-%m-%Y %I:%M %p",
    "%d/%m/%y %I:%M %p",
    "%d/%m/%Y %I:%M %p",
    "%Y-%m-%d %I:%M %p",
    "%d %b %Y %I:%M %p",
    "%Y/%m/%d",
    "%m/%d/%Y",
]
DATE_FORMATS = sorted(_FORMATS, key=lambda f: -len(f.split()))

# ab-325: labels a statement prints its opening balance and totals under,
# used when its fees come off the balance without rows of their own.
_OPENING_LABELS = ["Opening Balance", "Balance Brought Forward", "Balance B/F"]
_TOTALS_LABELS = [
    ("Total Credit", "Total Debit"),
    ("Total Credits", "Total Debits"),
    ("Total Money In", "Total Money Out"),
    ("Money In", "Money Out"),
    ("Paid In", "Paid Out"),
    ("Total In", "Total Out"),
    ("Payments In", "Payments Out"),
    ("Total Received", "Total Sent"),
]

# Heading words -> role, checked in this order (balance before "in").
_ROLE_WORDS = [
    ("balance", {"balance", "bal", "running balance"}),
    ("status", {"status", "transaction status"}),
    ("reference", {"reference", "ref", "receipt", "receipt no", "cheque", "transaction id", "ref no", "payment reference"}),
    ("date", {"date", "time", "completion time", "posting date", "transaction date", "trans date", "txn date"}),
    (
        "description",
        {"details", "description", "narration", "narrative", "particulars", "remarks", "memo", "transaction details",
         "transaction type and details", "transaction type"},
    ),
    (
        "money_out",
        {"debit", "debits", "withdrawn", "withdrawal", "withdrawals", "paid out", "money out", "out", "dr", "sent",
         "spent", "outflow", "payments out"},
    ),
    (
        "money_in",
        {"credit", "credits", "paid in", "deposit", "deposits", "money in", "in", "cr", "received", "inflow",
         "payments in"},
    ),
    ("signed_amount", {"amount", "transaction amount"}),
]
_SINGLE = {"date", "description", "balance", "reference", "status", "signed_amount", "money_in", "money_out"}

# Words shown in the wizard's page drawing - page 1 only, at most this many.
MAX_SNAPSHOT_WORDS = 1500
MAX_SNAPSHOT_ROWS = 30


def _role_for(text: str) -> str:
    """The role a heading suggests: whole heading first, then any word."""
    heading = normalise(text).replace("(", " ").replace(")", " ")
    heading = " ".join(heading.split())
    for role, words in _ROLE_WORDS:
        if heading in words:
            return role
    tokens = set(heading.split())
    for role, words in _ROLE_WORDS:
        if any(" " not in w and w in tokens for w in words):
            return role
    return "ignore"


def dedupe_roles(roles: list[str]) -> list[str]:
    """A single-use role keeps only its first column (later ones become
    "ignore"), and a signed amount gives way to money in/out columns."""
    out, used = [], set()
    for role in roles:
        if role in _SINGLE and role in used:
            role = "ignore"  # e.g. "Value Date" after "Date"
        used.add(role)
        out.append(role)
    if "signed_amount" in used and ({"money_in", "money_out"} & used):
        out = ["ignore" if r == "signed_amount" else r for r in out]
    return out


def _assign_roles(headings: list[str]) -> list[str]:
    return dedupe_roles([_role_for(heading) for heading in headings])


def _layout_word(text: str) -> str | None:
    """The leading letters-only words of a heading, as profile text (no
    digits or brackets): "Credit (Money In)" -> "Credit"."""
    words = []
    for word in text.split():
        if not word.isalpha():
            break
        words.append(word)
    out = " ".join(words[:5])[:40]
    return out or None


def _date_format(samples: list[str]) -> str | None:
    samples = [s for s in samples if s.strip()][:15]
    if not samples:
        return None
    for fmt in DATE_FORMATS:
        needed = len(fmt.split())
        try:
            for s in samples:
                datetime.strptime(" ".join(s.split()[:needed]), fmt)
        except ValueError:
            continue
        return fmt
    return None


def _newest_first(samples: list[str], fmt: str) -> bool:
    needed = len(fmt.split())
    try:
        dates = [datetime.strptime(" ".join(s.split()[:needed]), fmt) for s in samples if s.strip()]
    except ValueError:
        return False
    return len(dates) >= 2 and dates[0] > dates[-1]


def _decimal_comma(samples: list[str]) -> bool:
    """1.234,56 rather than 1,234.56."""
    return any(s.count(",") == 1 and s.rsplit(",", 1)[1].isdigit() and len(s.rsplit(",", 1)[1]) == 2
               and "." in s.rsplit(",", 1)[0] for s in samples)


def _finish(profile: dict) -> dict:
    """The guess as a full profile when it already is one; otherwise the
    draft as it stands (e.g. no amount column recognised) - the wizard
    still gets the columns and fixes the roles, and nothing is stored
    until a mapping passes the check."""
    try:
        return validate_profile(profile).model_dump(mode="json")
    except InvalidStatementProfile:
        return profile


# --- PDF ----------------------------------------------------------------------


def _pdf_guess(pdf) -> tuple[dict, list[str]] | None:
    page = pdf.pages[0]
    lines = page_lines(page)
    width = float(page.width)
    body = next((i for i, line in enumerate(lines) if is_body_line(line)), None)
    if body is None:
        return None
    header_at = _header_index(lines, body, width)
    if header_at is None:
        return None
    clusters = _header_clusters(lines, header_at, body)
    head_lines = len({round(w.top) for c in clusters for w in c.words})

    # Body lines (date + money) on page 1: column edges, date samples and
    # amount style.
    body_lines = [line for line in lines[body:] if is_body_line(line)]
    spans = sorted((w.x0, w.x1) for line in body_lines for w in line.words)

    columns, edges = [], []
    for n, cluster in enumerate(clusters):
        left = 0.0 if n == 0 else edges[-1][1]
        right = width if n == len(clusters) - 1 else _column_edge(cluster.x1, clusters[n + 1].x0, spans)
        edges.append((left, right))
        columns.append({"header": _layout_word(cluster.text), "text": cluster.text})
    roles = _assign_roles([c["text"] for c in columns])
    date_col = next((i for i, r in enumerate(roles) if r == "date"), None)
    date_samples = [_cell(line, edges, date_col) for line in body_lines] if date_col is not None else []
    if not any(date_samples):
        date_samples = [" ".join(w.text for w in line.words if DATE_TOKEN.match(w.text))[:30] for line in body_lines]
    fmt = _date_format(date_samples)
    if fmt is None:
        return None
    money = [w.text for line in body_lines for w in line.words if MONEY_TOKEN.match(w.text)]
    examples = [next((cell for line in body_lines if (cell := _cell(line, edges, n))), "") for n in range(len(edges))]

    out_columns = []
    for column, role, (x0, x1) in zip(columns, roles, edges, strict=True):
        entry = {"role": role, "x0": round(x0 / width, 4), "x1": round(min(x1, width) / width, 4)}
        if column["header"]:
            entry["header"] = column["header"]
        out_columns.append(entry)
    # Every column needs its heading found to place the table; one we
    # can't express (digits only) is dropped from the heading match.
    repeats = len(pdf.pages) < 2 or _has_heading(pdf.pages[1], [c.get("header") for c in out_columns])
    profile = {
        "file_type": "pdf",
        "date_format": fmt,
        "amounts": {"decimal_mark": ",", "thousands_sep": "."} if _decimal_comma(money) else {},
        "table": {
            "columns": out_columns,
            "column_bounds": "explicit",
            "header_lines": min(max(head_lines, 1), 3),
            "header_repeats_each_page": repeats,
            "row_order": "newest_first" if _newest_first(date_samples, fmt) else "oldest_first",
        },
    }
    return _finish(profile), examples


def _column_edge(left: float, right: float, spans: list[tuple[float, float]]) -> float:
    """Where one column ends and the next begins, between two headings: the
    middle of the widest gap no value crosses (headings are often centred
    while values are left-aligned - ab-325), or halfway between the
    headings when the values leave no gap."""
    if right <= left:
        return (left + right) / 2
    best, start = None, left
    for x0, x1 in spans:
        if x1 <= start:
            continue
        if x0 >= right:
            break
        if x0 > start and (best is None or x0 - start > best[1] - best[0]):
            best = (start, x0)
        start = max(start, x1)
    if start < right and (best is None or right - start > best[1] - best[0]):
        best = (start, right)
    if best is None or best[1] - best[0] < 2:
        return (left + right) / 2
    return (best[0] + best[1]) / 2


def _cell(line: Line, edges: list[tuple[float, float]], col: int) -> str:
    x0, x1 = edges[col]
    return " ".join(w.text for w in line.words if x0 <= (w.x0 + w.x1) / 2 <= x1)


def _has_heading(page, headers: list[str | None]) -> bool:
    words = {normalise(w.text) for line in page_lines(page) for w in line.words}
    wanted = [normalise(h).split()[0] for h in headers if h]
    return bool(wanted) and all(w in words for w in wanted)


def _pdf_snapshot(pdf) -> dict:
    page = pdf.pages[0]
    lines = page_lines(page)
    count, out = 0, []
    for index, line in enumerate(lines):
        words = []
        for w in line.words:
            if count >= MAX_SNAPSHOT_WORDS:
                break
            words.append({"text": w.text, "x0": round(w.x0, 1), "x1": round(w.x1, 1)})
            count += 1
        out.append({"index": index, "top": round(line.top, 1), "words": words})
    return {
        "page": {"width": round(float(page.width), 1), "height": round(float(page.height), 1), "lines": out},
        "page_count": len(pdf.pages),
    }


# --- CSV / XLSX -----------------------------------------------------------------


def _grid_header(grid: list[list]) -> int | None:
    body = next(
        (i for i, row in enumerate(grid) if any(_date_like(v) for v in row) and any(_money_like(v) for v in row)),
        None,
    )
    for i in range(body - 1 if body is not None else len(grid) - 1, -1, -1):
        texts = [v for v in grid[i] if v not in (None, "")]
        if len(texts) >= 3 and all(isinstance(v, str) and not any(ch.isdigit() for ch in v) for v in texts):
            return i
    return None


def _grid_guess(grid: list[list], file_type: str) -> tuple[dict, list[str]] | None:
    header_at = _grid_header(grid)
    if header_at is None:
        return None
    cells = [(i, v) for i, v in enumerate(grid[header_at]) if isinstance(v, str) and v.strip()]
    roles = _assign_roles([v for _, v in cells])
    columns = []
    for (index, text), role in zip(cells, roles, strict=True):
        heading = " ".join(text.split())
        if role == "ignore" or not heading or len(heading) > 40 or any(ch.isdigit() for ch in heading):
            continue
        columns.append((index, {"role": role, "header": heading}))
    date_index = next((i for i, c in columns if c["role"] == "date"), None)
    body = [row for row in grid[header_at + 1 :] if any(v not in (None, "") for v in row)][:30]
    samples = []
    for row in body:
        value = row[date_index] if date_index is not None and date_index < len(row) else None
        samples.append(value.strftime("%d/%m/%Y") if hasattr(value, "strftime") else str(value or ""))
    fmt = _date_format(samples) or "%d/%m/%Y"
    if date_index is not None and all(hasattr(row[date_index], "strftime") for row in body if date_index < len(row)):
        fmt = "%d/%m/%Y"  # real spreadsheet dates: the format is only for text cells
    profile = {
        "file_type": file_type,
        "date_format": fmt,
        "table": {
            "columns": [c for _, c in columns],
            "column_bounds": "cells",
            "row_order": "newest_first" if _newest_first(samples, fmt) else "oldest_first",
        },
    }
    examples = []
    for index, _ in columns:
        value = next((row[index] for row in body if index < len(row) and row[index] not in (None, "")), "")
        examples.append(value.strftime("%d/%m/%Y") if hasattr(value, "strftime") else str(value))
    return _finish(profile), examples


def _grid_snapshot(grid: list[list]) -> dict:
    rows = []
    for row in grid[:MAX_SNAPSHOT_ROWS]:
        rows.append(["" if v is None else (v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else str(v)) for v in row])
    # Spreadsheets pad rows to the widest one: keep only used columns.
    width = max((i + 1 for row in rows for i, v in enumerate(row) if v != ""), default=0)
    return {"rows": [row[:width] for row in rows], "header_row": _grid_header(grid)}


# --- fees with no row of their own (ab-325) ------------------------------------


def _passes(content: bytes, profile: dict) -> bool:
    try:
        result = read_statement(content, validate_profile(profile), default_currency="KES")
    except Exception:
        return False
    return result.check.passed


def _with_unlisted_charges(content: bytes, profile: dict, lines: list[Line]) -> dict:
    """A guess whose balances don't add up gets one more try: the statement's
    printed opening balance and totals, with fees that have no row of their
    own added as charges. Kept only if that passes; otherwise the guess is
    returned as it was."""
    if _passes(content, profile):
        return profile
    opening = next((label for label in _OPENING_LABELS if _labelled_amount(lines, label)), None)
    totals = next(
        ((i, o) for i, o in _TOTALS_LABELS if _labelled_amount(lines, i) and _labelled_amount(lines, o)), None
    )
    if not opening or not totals:
        return profile
    tried = {
        **profile,
        "opening_balance": {"source": "labelled_line", "label": opening},
        "totals": {"source": "labelled_lines", "label": totals[0], "out_label": totals[1]},
        "unlisted_charges": True,
    }
    return tried if _passes(content, tried) else profile


def _labelled_amount(lines: list[Line], label: str) -> bool:
    wanted = normalise(label).split()
    for line in lines:
        tokens = [normalise(w.text) for w in line.words]
        for i in range(len(tokens) - len(wanted) + 1):
            if tokens[i : i + len(wanted)] == wanted and any(
                MONEY_TOKEN.match(w.text) for w in line.words[i + len(wanted) :]
            ):
                return True
    return False


# --- entry point ------------------------------------------------------------------


def layout_start(content: bytes, file_type: str) -> dict:
    """{"guess": profile | None, "examples": one sample cell per guessed
    column, "page": ... (PDF) | "rows": ... (CSV/XLSX)}. Raises only for a
    file that can't be opened at all."""
    if file_type == "pdf":
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            snapshot = _pdf_snapshot(pdf)
            try:
                found = _pdf_guess(pdf)
                first_page = page_lines(pdf.pages[0])
            except Exception:
                found = None
        if found is not None:
            try:
                found = (_with_unlisted_charges(content, found[0], first_page), found[1])
            except Exception:
                pass
        return {"guess": found[0] if found else None, "examples": found[1] if found else [], **snapshot}
    grid = load_grid(content, file_type)
    try:
        found = _grid_guess(grid[:500], file_type)
    except Exception:
        found = None
    return {"guess": found[0] if found else None, "examples": found[1] if found else [], **_grid_snapshot(grid)}
