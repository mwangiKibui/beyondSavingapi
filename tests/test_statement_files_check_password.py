import io
from pathlib import Path

import pypdf
import pytest

from app.services.statement_files import IncorrectStatementPassword, check_password, decrypt_if_needed

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


def test_decrypt_if_needed_returns_unencrypted_pdf_unchanged():
    original = UNENCRYPTED_PDF.read_bytes()
    result = decrypt_if_needed(content=original, filename="member_statement_sample.pdf", password=None)
    assert result == original


def test_decrypt_if_needed_returns_non_pdf_content_unchanged():
    result = decrypt_if_needed(content=b"not really a pdf", filename="statement.docx", password=None)
    assert result == b"not really a pdf"


def test_decrypt_if_needed_strips_encryption_from_an_encrypted_pdf():
    result = decrypt_if_needed(
        content=ENCRYPTED_PDF.read_bytes(),
        filename="statement_sample.pdf",
        password=ENCRYPTED_PDF_PASSWORD,
    )

    reader = pypdf.PdfReader(io.BytesIO(result))
    assert not reader.is_encrypted
    # Readable with no password at all now - the whole point.
    assert "M-PESA STATEMENT" in reader.pages[0].extract_text()
