import io
import warnings
import zipfile
from typing import Literal

import pikepdf


class IncorrectStatementPassword(Exception):
    pass


def _open_pdf(content: bytes, password: str | None) -> pikepdf.Pdf:
    # pikepdf (backed by qpdf), not pypdf: pypdf 5.0.1 fails to validate a
    # genuinely correct password against at least one real-world Equity
    # Bank statement (AES-128, /V 4 /R 4) - confirmed by hand, pikepdf
    # opens the same file with the same password without issue. A
    # password harmlessly passed to an unencrypted PDF just warns, so
    # that's silenced rather than treated as a problem.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        return pikepdf.open(io.BytesIO(content), password=password or "")


def check_password(*, content: bytes, filename: str, password: str | None) -> None:
    """Verifies a statement file's password, if it needs one.

    Only PDF encryption is checked - no Word (.doc/.docx) password-unlock
    library has been evaluated yet (every real sample analyzed so far,
    see docs/provider-onboarding-runbook.md, has been a PDF), so a Word
    file is accepted without a password check for now. An unencrypted
    PDF (e.g. Mentor Sacco) is also accepted without one, regardless of
    whether a password was supplied.
    """
    if not filename.lower().endswith(".pdf"):
        return

    try:
        _open_pdf(content, password).close()
    except pikepdf.PasswordError as exc:
        raise IncorrectStatementPassword() from exc


def decrypt_if_needed(*, content: bytes, filename: str, password: str | None) -> bytes:
    """Strips PDF encryption from content before it's stored, so nothing
    downstream (a parser, weeks from now) ever needs the password again -
    we already said "we do not save the password," and this is what makes
    that true without also making the file unreadable forever after.

    Assumes check_password() has already been called and passed - this
    doesn't re-validate the password, just re-applies it to produce a
    plain copy. Passes non-PDFs and already-unencrypted PDFs through
    unchanged.
    """
    if not filename.lower().endswith(".pdf"):
        return content

    pdf = _open_pdf(content, password)
    try:
        if not pdf.is_encrypted:
            return content

        buffer = io.BytesIO()
        pdf.save(buffer)
        return buffer.getvalue()
    finally:
        pdf.close()


# --- ab-319: which files are accepted ----------------------------------------

FileType = Literal["pdf", "csv", "xlsx"]
_EXTENSIONS: dict[str, FileType] = {".pdf": "pdf", ".csv": "csv", ".xlsx": "xlsx"}
UNSUPPORTED_FILE_MESSAGE = (
    "This file type can't be read. Download your statement as a PDF, CSV or Excel (.xlsx) file."
)


class UnreadableStatementFile(Exception):
    """The file's contents don't match its type (e.g. a renamed file)."""


def file_type_of(file_name: str) -> FileType | None:
    """pdf / csv / xlsx from the name; None for anything else (.doc,
    .docx and old .xls are no longer accepted)."""
    lower = (file_name or "").lower()
    return next((t for ext, t in _EXTENSIONS.items() if lower.endswith(ext)), None)


def decode_csv(content: bytes) -> str:
    """UTF-8 (with or without a byte-order mark), else Windows-1252 /
    Latin-1 - what banks' CSV exports use."""
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UnreadableStatementFile("This CSV file couldn't be read")


def check_file_contents(content: bytes, file_type: FileType) -> None:
    if file_type == "pdf":
        if not content.lstrip()[:5].startswith(b"%PDF"):
            raise UnreadableStatementFile("This file isn't a PDF")
    elif file_type == "csv":
        if not content.strip() or b"\x00" in content or content.startswith(b"%PDF") or content.startswith(b"PK"):
            raise UnreadableStatementFile("This file isn't a CSV file")
        decode_csv(content)
    elif file_type == "xlsx":
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                if "xl/workbook.xml" not in z.namelist():
                    raise UnreadableStatementFile("This file isn't an Excel (.xlsx) file")
        except zipfile.BadZipFile:
            raise UnreadableStatementFile("This file isn't an Excel (.xlsx) file") from None
