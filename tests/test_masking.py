"""ab-314: the masked page the AI layout proposer sees. Synthetic PDFs only."""

import re

from app.parsers.profiles.masking import mask_pdf, shape
from tests.statement_pdfs import Layout, build

SECRET_NAME = "WANJIRU KIPROTICH"
SECRET_PHONE = "0712345678"
SECRET_ACCOUNT = "ACC-77441209"


def _rows():
    return [
        ["01/10/2026", "Payment from Wanjiru Kiprotich", "1,500.00", "", "11,500.00"],
        ["02/10/2026", "Paid to Shop Zeta 0712345678", "", "250.00", "11,250.00"],
        ["03/10/2026", "Transfer to Otieno Baraka", "", "3,000.00", "8,250.00"],
    ]


def _statement(**kwargs) -> bytes:
    layout = Layout(
        labels=[("Account Name", SECRET_NAME), ("Account Number", SECRET_ACCOUNT), ("Statement Period", "01/10/2026 - 31/10/2026")],
        name_line_above_table=SECRET_NAME,
        post_rows=[["", "Total Paid In", "1,500.00", "3,250.00", ""]],
        **kwargs,
    )
    return build(_rows(), layout)


def test_shape_keeps_only_the_form():
    assert shape("03-09-26 08:15 AM") == "99-99-99 99:99 XX"
    assert shape("Kamau") == "Xxxxx"
    assert shape("2,000.00") == "9,999.99"
    assert shape("(1,250.50)") == "(9,999.99)"


def test_no_name_number_or_amount_survives():
    masked = mask_pdf(_statement())
    assert masked is not None
    lowered = masked.lower()
    for secret in ["wanjiru", "kiprotich", "otieno", "baraka", "zeta", SECRET_PHONE, "77441209"]:
        assert secret.lower() not in lowered, secret
    # No real amount, date or any digit other than the page marker's.
    # Each line's vertical position (ab-333) is layout, not a value: left out here.
    body = "\n".join(_text(line) for line in masked.splitlines() if not line.startswith("--- page"))
    assert not re.search(r"[0-8]", body), "only 9s may stand for digits"
    for amount in ["1,500.00", "11,500.00", "250.00", "3,000.00"]:
        assert amount not in masked


def test_headings_and_statement_words_stay_readable():
    masked = mask_pdf(_statement())
    for heading in ["Date", "Details", "Money In", "Money Out", "Balance"]:
        assert heading in masked
    assert "Total Paid In" in masked
    assert "Account Name:" in masked
    assert "Statement Period:" in masked
    # Values keep their shape, so the model can tell dates from amounts.
    assert "99/99/9999" in masked
    assert "9,999.99" in masked
    # Words outside the statement vocabulary become shapes; "from" is kept.
    assert "Xxxxxxx from Xxxxxxx Xxxxxxxxx" in masked


def test_columns_keep_their_positions():
    masked = mask_pdf(_statement())
    lines = masked.splitlines()
    header = next(line for line in lines if "Money In" in line)
    row = next(line for line in lines if _text(line).lstrip().startswith("99/99/9999") and "9,999.99" in line)
    # The money-in amount sits under its heading (within a few characters).
    assert abs(row.index("9,999.99") - header.index("Money In")) <= 6


def test_each_line_says_where_it_sits_on_the_page():
    lines = [line for line in mask_pdf(_statement()).splitlines() if not line.startswith("--- page")]
    tops = [int(line.split("|", 1)[0]) for line in lines]
    assert tops == sorted(tops) and len(set(tops)) == len(tops)


def _text(line: str) -> str:
    return re.sub(r"^\s*\d+\| ", "", line)


def test_a_wrapped_heading_stays_verbatim():
    masked = mask_pdf(
        build(
            _rows(),
            Layout(
                headers=["Txn Date", "Description", "Credit", "Debit", "Balance"],
                header_second_line={0: "Value Date"},
            ),
        )
    )
    assert "Txn Date" in masked and "Value Date" in masked


def test_only_the_asked_pages():
    rows = [[f"{d:02d}/10/2026", "Payment", "10.00", "", f"{100 + d}.00"] for d in range(1, 29)]
    pdf = build(rows, Layout(rows_per_page=10))
    one = mask_pdf(pdf)
    two = mask_pdf(pdf, pages=2)
    assert one.count("--- page") == 1
    assert two.count("--- page") == 2


def test_not_a_pdf():
    assert mask_pdf(b"not a pdf") is None


def test_a_spreadsheet_keeps_its_heading_row_and_shapes_the_rest():
    from datetime import date

    from app.parsers.profiles.masking import mask_grid

    grid = [
        ["Mobile statement for", "Otieno Baraka", None],
        ["Date", "Particulars", "Withdrawn", "Paid In", "Balance"],
        [date(2026, 10, 1), "Paid to Wanjiru Kiprotich", 250.5, None, 1000],
        ["02/10/2026", "Received from 0712345678", None, "1,000.00", "2,000.00"],
    ]
    masked = mask_grid(grid)
    assert "Date | Particulars | Withdrawn | Paid In | Balance" in masked
    for secret in ["Otieno", "Baraka", "Wanjiru", "Kiprotich", "0712345678", "250.5", "1,000.00"]:
        assert secret not in masked, secret
    assert "9999-99-99" in masked and "99/99/9999" in masked and "9,999.99" in masked
    assert mask_grid([]) is None


def test_page_footer_words_stay_readable_and_its_values_dont():
    # ab-337: the model can name a footer ("Printed On:") to skip; the
    # printer's name and the time it was printed stay shapes.
    masked = mask_pdf(_statement(page_footer="Printed On: Thu,26 November 2026 10:41: Printed By: Wanjiru Page:{page} of {pages}"))
    assert "Printed On:" in masked
    assert "Printed By:" in masked
    assert "Wanjiru" not in masked and "10:41" not in masked


def test_the_bottom_of_a_long_page_is_kept(monkeypatch):
    from app.parsers.profiles import masking

    monkeypatch.setattr(masking, "MAX_LINES", 12)
    rows = [[f"{d:02d}/10/2026", "Paid to Store", "", "1.00", f"{1000 - d}.00"] for d in range(1, 21)]
    pdf = build(rows, Layout(page_footer="Printed On: Thu Page {page} of {pages}"))
    masked = mask_pdf(pdf)
    assert len(masked.splitlines()) <= masking.MAX_LINES + 1  # + the page marker
    assert "Printed On:" in masked
