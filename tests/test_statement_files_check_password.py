from pathlib import Path

import pytest

from app.services.statement_files import IncorrectStatementPassword, check_password

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "statements"
UNENCRYPTED_PDF = FIXTURES_DIR / "mentor_sacco" / "member_statement_sample.pdf"
ENCRYPTED_PDF = FIXTURES_DIR / "mpesa" / "statement_sample.pdf"
ENCRYPTED_PDF_PASSWORD = "0000000"


def test_check_password_accepts_unencrypted_pdf_without_password():
    check_password(
        content=UNENCRYPTED_PDF.read_bytes(), filename="member_statement_sample.pdf", password=None
    )


def test_check_password_accepts_unencrypted_pdf_even_with_a_password_supplied():
    check_password(
        content=UNENCRYPTED_PDF.read_bytes(),
        filename="member_statement_sample.pdf",
        password="irrelevant",
    )


def test_check_password_rejects_encrypted_pdf_with_no_password():
    with pytest.raises(IncorrectStatementPassword):
        check_password(
            content=ENCRYPTED_PDF.read_bytes(), filename="statement_sample.pdf", password=None
        )


def test_check_password_rejects_encrypted_pdf_with_wrong_password():
    with pytest.raises(IncorrectStatementPassword):
        check_password(
            content=ENCRYPTED_PDF.read_bytes(), filename="statement_sample.pdf", password="wrongpass"
        )


def test_check_password_accepts_encrypted_pdf_with_correct_password():
    check_password(
        content=ENCRYPTED_PDF.read_bytes(),
        filename="statement_sample.pdf",
        password=ENCRYPTED_PDF_PASSWORD,
    )


def test_check_password_skips_check_for_non_pdf_files():
    # No Word (.doc/.docx) password-unlock library has been evaluated yet -
    # a Word file is accepted without a password check for now.
    check_password(content=b"not really a pdf", filename="statement.docx", password=None)
