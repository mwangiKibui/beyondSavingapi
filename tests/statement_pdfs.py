"""Synthetic statement PDFs for the ab-312 tests, built in memory with
reportlab. Names, numbers and dates are made up."""

import io
from dataclasses import dataclass, field
from datetime import date, timedelta

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


@dataclass
class Layout:
    title: str = "Account Statement"
    labels: list[tuple[str, str]] = field(
        default_factory=lambda: [("Account Name", "ALEX SAMPLE"), ("Statement Period", "01/10/2026 - 31/10/2026")]
    )
    headers: list[str] = field(default_factory=lambda: ["Date", "Details", "Money In", "Money Out", "Balance"])
    # Left edge of each column (points) - must match headers.
    xs: list[float] = field(default_factory=lambda: [40, 110, 330, 410, 490])
    # Where each heading starts, when not over its values' left edge
    # (headings centred over left-aligned values - ab-325).
    header_xs: list[float] | None = None
    ruled: bool = False
    # Second header line under some columns: {column index: text}.
    header_second_line: dict[int, str] = field(default_factory=dict)
    name_line_above_table: str | None = None
    rows_per_page: int = 25
    page_number_lines: bool = True
    end_marker: str | None = "End of statement"
    pre_rows: list[list[str]] = field(default_factory=list)  # e.g. an opening balance row
    post_rows: list[list[str]] = field(default_factory=list)  # e.g. a totals row
    repeat_header: bool = True
    # Text under the description column just below the table (a footer).
    footer: str | None = None
    # ab-333: where a row's description sits around its date line - on it
    # ("on"), on the line above it ("above", text printed over the date like
    # a vertically centred cell) or split above and below ("around").
    text_position: str = "on"


def build(rows: list[list[str]], layout: Layout | None = None, *, continuation: dict[int, str] | None = None) -> bytes:
    """rows: one list of cell texts per transaction, in header order.
    continuation: {row index: extra description text on a second line}."""
    layout = layout or Layout()
    continuation = continuation or {}
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    all_rows = [("pre", r) for r in layout.pre_rows] + [("row", r) for r in rows] + [("post", r) for r in layout.post_rows]
    chunks = [all_rows[i : i + layout.rows_per_page] for i in range(0, len(all_rows), layout.rows_per_page)] or [[]]
    desc_col = next((i for i, h in enumerate(layout.headers) if h.lower() in {"details", "description", "particulars"}), 1)
    row_index = 0
    for page_no, chunk in enumerate(chunks, start=1):
        y = height - 50
        if layout.page_number_lines:
            c.setFont("Helvetica", 8)
            c.drawString(width - 90, height - 30, f"Page {page_no} of {len(chunks)}")
        if page_no == 1:
            c.setFont("Helvetica-Bold", 16)
            c.drawString(40, y, layout.title)
            y -= 30
            c.setFont("Helvetica", 9)
            for label, value in layout.labels:
                c.drawString(40, y, f"{label}:")
                c.drawString(160, y, value)
                y -= 14
            y -= 10
            if layout.name_line_above_table:
                c.drawString(40, y, layout.name_line_above_table)
                y -= 16
        if page_no == 1 or layout.repeat_header:
            c.setFont("Helvetica-Bold", 9)
            for x, header in zip(layout.header_xs or layout.xs, layout.headers, strict=True):
                c.drawString(x + 2, y, header)
            if layout.header_second_line:
                for col, text in layout.header_second_line.items():
                    c.drawString(layout.xs[col] + 2, y - 11, text)
                y -= 11
            top_of_table = y + 12
            y -= 16
        else:
            top_of_table = y + 12
        c.setFont("Helvetica", 9)
        for kind, cells in chunk:
            if kind == "row" and layout.text_position != "on":
                words = cells[desc_col].split()
                above = words if layout.text_position == "above" else words[: len(words) // 2 or 1]
                below = [] if layout.text_position == "above" else words[len(above) :]
                c.drawString(layout.xs[desc_col] + 2, y, " ".join(above))
                y -= 7
                for col, (x, text) in enumerate(zip(layout.xs, cells, strict=True)):
                    if text and col != desc_col:
                        c.drawString(x + 2, y, text)
                if below:
                    y -= 7
                    c.drawString(layout.xs[desc_col] + 2, y, " ".join(below))
                y -= 16
                row_index += 1
                continue
            for x, text in zip(layout.xs, cells, strict=True):
                if text:
                    c.drawString(x + 2, y, text)
            y -= 13
            if kind == "row" and row_index in continuation:
                c.drawString(layout.xs[desc_col] + 2, y, continuation[row_index])
                y -= 13
            if kind == "row":
                row_index += 1
        if layout.ruled:
            for x in [*layout.xs, width - 30]:
                c.line(x, top_of_table, x, y + 8)
        if layout.footer:
            c.drawString(layout.xs[desc_col] + 2, y - 2, layout.footer)
        if page_no == len(chunks) and layout.end_marker:
            c.drawString(40, y - 10, layout.end_marker)
        c.showPage()
    c.save()
    return buf.getvalue()


def sample_rows(n: int = 6, *, start_balance: float = 1000.0, day0: int = 1) -> list[list[str]]:
    """Alternating in/out rows with a correct running balance, oldest first."""
    rows, balance = [], start_balance
    for i in range(n):
        day = (date(2026, 10, day0) + timedelta(days=i)).strftime("%d/%m/%Y")
        amount = 100.0 + i * 10
        if i % 2 == 0:
            balance += amount
            cells = [day, f"Payment from Shop {chr(65 + i)}", f"{amount:,.2f}", "", f"{balance:,.2f}"]
        else:
            balance -= amount
            cells = [day, f"Paid to Store {chr(65 + i)}", "", f"{amount:,.2f}", f"{balance:,.2f}"]
        rows.append(cells)
    return rows
