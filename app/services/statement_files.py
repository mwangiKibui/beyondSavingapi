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
