"""Renders every Reports PDF export (ab-85, ab-152) - a formatted SNAPSHOT
of a report's data, NOT a transaction-level ledger. Kept as pure functions
(data in, PDF bytes out) so they're testable without a DB.

Branding (2026-10-02 feedback): the product is Centrail, not beyondSaving -
every PDF leads with a left-aligned "Centrail" wordmark (black "Cen",
green "trail") over a two-tone black-to-green rule, a centered report
title with the date range actually used, and brand-green table headers,
instead of reportlab's centered, uncolored defaults.
"""

import io
from datetime import date

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Flowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

# Same brand green as the shipped app's design system (docs/design-system.md
# - green-600, #16a34a, the only accent color) and a pale tint of it for
# summary-table cells, so an exported PDF reads as the same product as the
# app it came from. Kept as hex strings too (not just HexColor objects) -
# the wordmark needs them inline in Paragraph markup to get two colors in
# one line of text.
BRAND_GREEN_HEX = "#16a34a"
INK_HEX = "#1f2937"
BRAND_GREEN = colors.HexColor(BRAND_GREEN_HEX)
BRAND_GREEN_SOFT = colors.HexColor("#eafbf1")
INK = colors.HexColor(INK_HEX)
MUTED = colors.HexColor("#6b7280")
GRID_LINE = colors.HexColor("#e5e7eb")

PAGE_MARGINS = dict(topMargin=0.75 * inch, bottomMargin=0.75 * inch, leftMargin=0.75 * inch, rightMargin=0.75 * inch)

TABLE_STYLE = TableStyle(
    [
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("BACKGROUND", (0, 0), (-1, 0), BRAND_GREEN),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("TEXTCOLOR", (0, 1), (-1, -1), INK),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, BRAND_GREEN_SOFT]),
        ("GRID", (0, 0), (-1, -1), 0.5, GRID_LINE),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]
)


class _TwoToneRule(Flowable):
    """A horizontal rule that starts black and continues in brand green
    (2026-10-02 feedback) - HRFlowable only draws a single color, so the
    wordmark's underline is drawn by hand instead: one black segment, one
    green segment, split at the same proportion as "Cen"/"trail" in the
    wordmark above it (35%/65%) so the color change roughly lines up with
    the text above.
    """

    def __init__(self, thickness: float = 1.6, split: float = 0.35):
        super().__init__()
        self.thickness = thickness
        self.split = split
        self.width = 0

    def wrap(self, available_width: float, available_height: float) -> tuple[float, float]:
        self.width = available_width
        return available_width, self.thickness

    def draw(self) -> None:
        split_x = self.width * self.split
        self.canv.setLineWidth(self.thickness)
        self.canv.setStrokeColor(INK)
        self.canv.line(0, 0, split_x, 0)
        self.canv.setStrokeColor(BRAND_GREEN)
        self.canv.line(split_x, 0, self.width, 0)


def _styles() -> dict:
    base = getSampleStyleSheet()
    return {
        "brand": ParagraphStyle(
            "Brand", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=18,
            alignment=TA_LEFT,
        ),
        "title": ParagraphStyle(
            "ReportTitle", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=14,
            textColor=INK, alignment=TA_CENTER, spaceAfter=4,
        ),
        "meta": ParagraphStyle(
            "Meta", parent=base["Normal"], fontSize=9.5, textColor=MUTED, alignment=TA_CENTER,
        ),
        "heading": ParagraphStyle(
            "SectionHeading", parent=base["Heading2"], fontSize=12, textColor=INK, alignment=TA_LEFT,
        ),
        "body": ParagraphStyle("Body", parent=base["Normal"], alignment=TA_LEFT, textColor=INK),
    }


def _report_title(report_name: str, from_date: date | None, to_date: date | None) -> str:
    """"<Report name> Report from <from> to <to>" - the exact phrasing
    requested, with graceful fallbacks for a report that has no date
    filter at all (Account Summary) versus one whose date filter simply
    wasn't set for this export."""
    if from_date is None and to_date is None:
        return f"{report_name} Report"
    if from_date is not None and to_date is not None:
        return f"{report_name} Report from {from_date.isoformat()} to {to_date.isoformat()}"
    if from_date is not None:
        return f"{report_name} Report from {from_date.isoformat()} onward"
    return f"{report_name} Report through {to_date.isoformat()}"


def _filter_description(*, account_name: str | None, from_date: date | None, to_date: date | None) -> str | None:
    """"For Account <Account>, From <from> To <to>" (2026-10-02 feedback) -
    reflects whatever filter was actually applied, directly below the
    title. Returns None (render nothing) when no filter was applied at
    all, rather than claiming a scope the export doesn't actually have.
    """
    parts: list[str] = []
    if account_name:
        parts.append(f"For Account {account_name}")
    if from_date is not None or to_date is not None:
        start = from_date.isoformat() if from_date else "the start"
        end = to_date.isoformat() if to_date else "today"
        date_part = f"From {start} To {end}"
        parts.append(f", {date_part}" if parts else date_part)
    return "".join(parts) if parts else None


def _brand_header(styles: dict) -> list:
    return [
        Paragraph(f'<font color="{INK_HEX}">Cen</font><font color="{BRAND_GREEN_HEX}">trail</font>', styles["brand"]),
        Spacer(1, 12),
        _TwoToneRule(),
        Spacer(1, 14),
    ]


def build_report_pdf(
    report: dict,
    *,
    account_name: str | None,
    from_date: date | None,
    to_date: date | None,
    direction: str | None,
) -> bytes:
    """`report` is exactly build_report()'s own return shape (the same
    data GET /reports serves). account_name/from_date/to_date/direction
    are the resolved filters, shown as a summary line so the export is
    self-describing."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, title="Centrail Report", **PAGE_MARGINS)
    styles = _styles()
    currency = report["currency"]

    filter_text = _filter_description(account_name=account_name, from_date=from_date, to_date=to_date)
    meta_parts = [filter_text] if filter_text else []
    meta_parts.append(f"Type: {direction or 'All'}")
    meta_parts.append(f"Currency: {currency}")

    elements = [
        *_brand_header(styles),
        Paragraph(_report_title("Report", from_date, to_date), styles["title"]),
        Paragraph(" &nbsp;|&nbsp; ".join(meta_parts), styles["meta"]),
        Spacer(1, 16),
    ]

    summary_data = [
        ["Total in", f"{report['total_in']} {currency}"],
        ["Total out", f"{report['total_out']} {currency}"],
        ["Net", f"{report['net']} {currency}"],
        ["Reconciled, no category", f"{report['reconciled_no_category_total']} {currency}"],
        ["Unreconciled", f"{report['unreconciled_total']} {currency}"],
    ]
    summary_table = Table([["Summary", ""], *summary_data], colWidths=[220, 220])
    summary_table.setStyle(TABLE_STYLE)
    elements.append(summary_table)
    elements.append(Spacer(1, 20))

    elements.append(Paragraph("By category", styles["heading"]))
    elements.append(Spacer(1, 6))

    by_category = report["by_category"]
    if by_category:
        table_data = [["Category", "Type", f"Total ({currency})"]]
        for item in by_category:
            table_data.append(
                [item["category_name"] or "—", item["category_type"] or "—", str(item["total"])]
            )
        category_table = Table(table_data, colWidths=[220, 120, 100])
        category_table.setStyle(TABLE_STYLE)
        elements.append(category_table)
    else:
        elements.append(Paragraph("No categorized allocations in this range.", styles["body"]))

    doc.build(elements)
    return buffer.getvalue()


def build_simple_table_pdf(
    title: str,
    column_headers: list[str],
    rows: list[list[str]],
    *,
    subtitle: str | None = None,
    account_name: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
) -> bytes:
    """Generic Centrail-branded title + table PDF, shared by every ab-152
    export (Account Summary, Budget Plans, Category/Expense/Income
    Summary, Transfers). `title` is the report's own name (e.g. "Budget
    Plans") - the page heading becomes "<title> Report[ from X to Y]" via
    _report_title. Pass account_name/from_date/to_date whenever the
    export actually has that filter applied (not every report has an
    account filter, and Account Summary has no date filter at all) - the
    line below the title (2026-10-02 feedback) then reads "For Account X"
    and/or "From Y To Z", reflecting only the filters actually in play.
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, title=f"Centrail - {title}", **PAGE_MARGINS)
    styles = _styles()

    elements = [
        *_brand_header(styles),
        Paragraph(_report_title(title, from_date, to_date), styles["title"]),
    ]
    filter_text = _filter_description(account_name=account_name, from_date=from_date, to_date=to_date)
    if filter_text:
        elements.append(Paragraph(filter_text, styles["meta"]))
    if subtitle:
        elements.append(Paragraph(subtitle, styles["meta"]))
    elements.append(Spacer(1, 16))

    if rows:
        table = Table([column_headers, *rows])
        table.setStyle(TABLE_STYLE)
        elements.append(table)
    else:
        elements.append(Paragraph("No data in this range.", styles["body"]))

    doc.build(elements)
    return buffer.getvalue()
