"""ab-319: which statement files are accepted, and that each really is
what its name says."""

import io

import pytest
from openpyxl import Workbook

from app.services.statement_files import (
    UNSUPPORTED_FILE_MESSAGE,
    UnreadableStatementFile,
    check_file_contents,
    file_type_of,
)
from tests.statement_pdfs import build, sample_rows


def xlsx_bytes(rows) -> bytes:
    wb = Workbook()
    for row in rows:
        wb.active.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.mark.parametrize(
    ("name", "expected"),
    [("s.pdf", "pdf"), ("S.PDF", "pdf"), ("export.csv", "csv"), ("Export.XLSX", "xlsx"), ("s.doc", None),
     ("s.docx", None), ("old.xls", None), ("s.txt", None), ("noext", None), ("", None)],
)
def test_file_type_comes_from_the_extension(name, expected):
    assert file_type_of(name) == expected


def test_the_unsupported_message_names_the_types_that_work():
    assert "PDF, CSV or Excel (.xlsx)" in UNSUPPORTED_FILE_MESSAGE


def test_real_files_pass():
    check_file_contents(build(sample_rows(2)), "pdf")
    check_file_contents(b"Date,Details,Amount\n01/10/2026,Shop,5.00\n", "csv")
    check_file_contents("﻿Date;Détails;Montant\n".encode() + b"01/10/2026;Caf\xc3\xa9;5,00\n", "csv")
    check_file_contents("Date,Details\n01/10/2026,Caf\xe9\n".encode("latin-1"), "csv")
    check_file_contents(xlsx_bytes([["Date", "Details", "Amount"], ["01/10/2026", "Shop", 5]]), "xlsx")


@pytest.mark.parametrize(
    ("content", "file_type"),
    [
        (b"Date,Amount\n", "pdf"),  # not a PDF
        (b"%PDF-1.4 binary\x00\x01", "csv"),  # a PDF renamed .csv
        (b"", "csv"),
        (b"PK\x03\x04 not really a zip", "xlsx"),
        (build(sample_rows(1)), "xlsx"),
    ],
)
def test_a_file_that_isnt_what_its_name_says_is_refused(content, file_type):
    with pytest.raises(UnreadableStatementFile):
        check_file_contents(content, file_type)


def test_a_zip_that_isnt_a_workbook_is_refused():
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<x/>")
    with pytest.raises(UnreadableStatementFile):
        check_file_contents(buf.getvalue(), "xlsx")
