"""Synthetic CSV / XLSX statements for the ab-319 tests. Made-up data."""

import csv
import io
from datetime import datetime

from openpyxl import Workbook

PREAMBLE = [["Account Name:", "ALEX SAMPLE"], ["Statement Period:", "01/10/2026 - 31/10/2026"], []]
HEADER = ["Date", "Details", "Money In", "Money Out", "Balance"]


def rows(n: int = 4, *, start: float = 1000.0) -> list[list]:
    """Typed rows (datetime and float cells), oldest first, running balance right."""
    out, balance = [], start
    for i in range(n):
        amount = 100.0 + 10 * i
        when = datetime(2026, 10, 1 + i)
        if i % 2 == 0:
            balance += amount
            out.append([when, f"Payment from Shop {chr(65 + i)}", amount, None, balance])
        else:
            balance -= amount
            out.append([when, f"Paid to Store {chr(65 + i)}", None, amount, balance])
    return out


def as_text(grid: list[list], *, decimal: str = ".") -> list[list[str]]:
    def text(v):
        if v is None:
            return ""
        if isinstance(v, datetime):
            return v.strftime("%d/%m/%Y")
        if isinstance(v, float):
            return f"{v:,.2f}" if decimal == "." else f"{v:,.2f}".replace(",", " ").replace(".", ",")
        return str(v)

    return [[text(v) for v in row] for row in grid]


def csv_bytes(grid: list[list], *, delimiter: str = ",", encoding: str = "utf-8", decimal: str = ".") -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter)
    for row in as_text(grid, decimal=decimal):
        writer.writerow(row)
    return buf.getvalue().encode(encoding)


def xlsx_bytes(grid: list[list], *, sheets: int = 1) -> bytes:
    wb = Workbook()
    for _ in range(sheets - 1):
        wb.create_sheet()
    target = wb.worksheets[sheets - 1]
    for row in grid:
        target.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
