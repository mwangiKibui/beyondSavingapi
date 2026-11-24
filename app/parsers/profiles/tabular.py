"""ab-319: CSV and XLSX statements as a grid of cells.

Both become list[list[value]] (strings, numbers, dates or None), capped
at MAX_ROWS x MAX_COLS. XLSX is read with openpyxl in read-only mode with
data_only=True - cached values only, formulas are never evaluated.
"""

import csv
import io
from datetime import date, datetime

from app.parsers.base import StatementParseError
from app.parsers.profiles.schema import StatementProfile
from app.parsers.profiles.text import normalise
from app.services.statement_files import UnreadableStatementFile, decode_csv

MAX_ROWS = 20_000
MAX_COLS = 30
_MONEY_ROLES = {"money_in", "money_out", "signed_amount", "balance"}


def _too_many() -> StatementParseError:
    return StatementParseError(f"This file has more than {MAX_ROWS:,} rows")


def _delimiter(text: str) -> str:
    """The candidate that splits the most of the first lines into the same
    number (>1) of fields - robust to comma decimals in ;-separated files,
    where csv.Sniffer guesses wrong."""
    sample = [line for line in text.splitlines()[:60] if line.strip()]
    best, best_score = ",", (0, 0)
    for candidate in (",", ";", "\t", "|"):
        counts = [len(r) for r in csv.reader(sample, delimiter=candidate)]
        widths = [c for c in counts if c > 1]
        if not widths:
            continue
        common = max(set(widths), key=widths.count)
        score = (widths.count(common), common)
        if score > best_score:
            best, best_score = candidate, score
    return best


def _load_csv(content: bytes) -> list[list]:
    try:
        text = decode_csv(content)
    except UnreadableStatementFile:
        raise StatementParseError("This CSV file couldn't be read") from None
    delimiter = _delimiter(text)
    grid = []
    for row in csv.reader(io.StringIO(text), delimiter=delimiter):
        if len(grid) >= MAX_ROWS:
            raise _too_many()
        grid.append([cell.strip() for cell in row[:MAX_COLS]])
    return grid


def _load_xlsx(content: bytes, sheet: int) -> list[list]:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception:
        raise StatementParseError("This Excel file couldn't be opened") from None
    try:
        if sheet >= len(wb.worksheets):
            raise StatementParseError("This Excel file doesn't have the sheet this layout reads")
        grid = []
        for row in wb.worksheets[sheet].iter_rows(values_only=True, max_col=MAX_COLS):
            if len(grid) >= MAX_ROWS:
                raise _too_many()
            grid.append([v.strip() if isinstance(v, str) else v for v in row])
        return grid
    finally:
        wb.close()


def load_grid(content: bytes, file_type: str, *, sheet: int = 0) -> list[list]:
    if file_type == "csv":
        return _load_csv(content)
    if file_type == "xlsx":
        return _load_xlsx(content, sheet)
    raise StatementParseError(f"Not a spreadsheet: {file_type}")


def cell_text(value, profile: StatementProfile, role: str | None) -> str:
    """A cell as the text the shared row logic expects: dates written in
    the profile's date format, amounts with its decimal mark."""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, datetime):
        return value.strftime(profile.date_format)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day).strftime(profile.date_format)
    if isinstance(value, (int, float)):
        if role in _MONEY_ROLES:
            return f"{value:.2f}".replace(".", profile.amounts.decimal_mark)
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    return str(value).strip()


def header_cells(row: list) -> list[tuple[int, str]]:
    """(column index, normalised text) of a row's non-empty text cells."""
    return [(i, normalise(v)) for i, v in enumerate(row) if isinstance(v, str) and v.strip()]


def find_header_row(grid: list[list], profile: StatementProfile) -> tuple[int, dict[int, int]] | None:
    """(row index, {profile column index: cell index}) of the first row
    holding every column heading the profile names."""
    wanted = [(i, normalise(c.header)) for i, c in enumerate(profile.table.columns) if c.header]
    for r, row in enumerate(grid):
        cells = header_cells(row)
        mapping: dict[int, int] = {}
        used: set[int] = set()
        for col, heading in wanted:
            index = next((i for i, text in cells if text == heading and i not in used), None)
            if index is None:
                break
            mapping[col] = index
            used.add(index)
        if wanted and len(mapping) == len(wanted):
            return r, mapping
    return None
