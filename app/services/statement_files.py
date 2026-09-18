import io
import warnings

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
