import io

import pypdf


class IncorrectStatementPassword(Exception):
    pass


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

    reader = pypdf.PdfReader(io.BytesIO(content))
    if not reader.is_encrypted:
        return

    if not password:
        raise IncorrectStatementPassword()

    # decrypt() returns a PasswordType: 0 (falsy) if the password didn't
    # work, 1 or 2 (truthy) if it matched the user or owner password.
    if not reader.decrypt(password):
        raise IncorrectStatementPassword()


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

    reader = pypdf.PdfReader(io.BytesIO(content))
    if not reader.is_encrypted:
        return content

    reader.decrypt(password or "")

    writer = pypdf.PdfWriter()
    for page in reader.pages:
        writer.add_page(page)

    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
