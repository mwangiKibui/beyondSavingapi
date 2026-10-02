"""Renders the ab-85 PDF export - a formatted SNAPSHOT of the aggregate
report (the same data GET /reports returns: the summary figures and the
by-category breakdown), NOT a transaction-level ledger. Kept as a pure
function (report dict + filter labels in, PDF bytes out) so it's testable
without a DB and reusable if another export surface ever wants the same
snapshot.
"""

import io
from datetime import date

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def _format_date_range(from_date: date | None, to_date: date | None) -> str:
    if from_date is None and to_date is None:
        return "All dates"
    start = from_date.isoformat() if from_date else "Start"
    end = to_date.isoformat() if to_date else "Today"
    return f"{start} to {end}"


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
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        title="beyondSaving Report",
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
    )
    styles = getSampleStyleSheet()
    currency = report["currency"]

    elements = [
        Paragraph("beyondSaving - Report", styles["Title"]),
        Paragraph(
            f"Account: {account_name or 'All accounts'} &nbsp;|&nbsp; "
            f"Date range: {_format_date_range(from_date, to_date)} &nbsp;|&nbsp; "
            f"Type: {direction or 'All'} &nbsp;|&nbsp; Currency: {currency}",
            styles["Normal"],
        ),
        Spacer(1, 16),
    ]

    summary_data = [
        ["Total in", f"{report['total_in']} {currency}"],
        ["Total out", f"{report['total_out']} {currency}"],
        ["Net", f"{report['net']} {currency}"],
        ["Reconciled, no category", f"{report['reconciled_no_category_total']} {currency}"],
        ["Unreconciled", f"{report['unreconciled_total']} {currency}"],
    ]
    summary_table = Table(summary_data, colWidths=[220, 220])
    summary_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ]
        )
    )
    elements.append(summary_table)
    elements.append(Spacer(1, 20))

    elements.append(Paragraph("By category", styles["Heading2"]))
    elements.append(Spacer(1, 6))

    by_category = report["by_category"]
    if by_category:
        table_data = [["Category", "Type", f"Total ({currency})"]]
        for item in by_category:
            table_data.append(
                [item["category_name"] or "—", item["category_type"] or "—", str(item["total"])]
            )
        category_table = Table(table_data, colWidths=[220, 120, 100])
        category_table.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        elements.append(category_table)
    else:
        elements.append(Paragraph("No categorized allocations in this range.", styles["Normal"]))

    doc.build(elements)
    return buffer.getvalue()
