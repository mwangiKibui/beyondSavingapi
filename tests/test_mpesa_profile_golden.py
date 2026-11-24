"""ab-313: the built-in M-Pesa profile must read statements exactly as the
bespoke parser does - every field, dedupe hash and row order."""

import os
from pathlib import Path

import pytest

from app.parsers.mpesa import parse_mpesa_statement
from app.parsers.profiles.builtin import load_builtin_profiles
from app.parsers.profiles.fingerprint import PageFingerprint, fingerprint_pdf
from app.parsers.profiles.matching import MATCH_THRESHOLD, similarity
from app.parsers.profiles.reader import read_statement
from app.parsers.profiles.schema import validate_profile
from app.services.statement_files import decrypt_if_needed

ROOT = Path(__file__).parent
FIXTURE = ROOT / "fixtures" / "statements" / "mpesa" / "statement_sample.pdf"
# The developer's real statement: never committed; its password only ever
# comes from the environment (MPESA_SAMPLE_PASSWORD).
REAL_SAMPLE = ROOT.parent.parent / "docs" / "sample-statements" / "mpesa_statement.pdf"


@pytest.fixture(scope="module")
def mpesa():
    builtin = next(b for b in load_builtin_profiles() if b.institution_name == "M-Pesa")
    return builtin


def _content(path: Path, password: str) -> bytes:
    return decrypt_if_needed(content=path.read_bytes(), filename=path.name, password=password)


def _assert_golden(content: bytes, builtin):
    expected = parse_mpesa_statement(content)
    result = read_statement(content, validate_profile(builtin.profile), default_currency="KES")
    assert result.check.passed, result.check.message
    assert len(result.transactions) == len(expected)
    for i, (got, want) in enumerate(zip(result.transactions, expected, strict=True)):
        assert got == want, f"row {i + 1} differs"
    fp = fingerprint_pdf(content)
    assert similarity(fp, PageFingerprint.from_text(builtin.fingerprint)) >= MATCH_THRESHOLD


def test_the_builtin_profile_is_valid_and_active(mpesa):
    validate_profile(mpesa.profile)
    assert mpesa.institution_type == "mobile_money"


def test_the_fixture_reads_exactly_as_the_parser_does(mpesa):
    _assert_golden(_content(FIXTURE, "0000000"), mpesa)


@pytest.mark.skipif(
    not (REAL_SAMPLE.exists() and os.environ.get("MPESA_SAMPLE_PASSWORD")),
    reason="needs the developer's real M-Pesa sample and MPESA_SAMPLE_PASSWORD",
)
def test_the_real_sample_reads_exactly_as_the_parser_does(mpesa):
    _assert_golden(_content(REAL_SAMPLE, os.environ["MPESA_SAMPLE_PASSWORD"]), mpesa)
