"""ab-313 / ab-320: each built-in profile must read statements exactly as
the bespoke parser it replaced - every field, dedupe hash and row order.
The parsers are kept only as references for these tests."""

import os
from pathlib import Path

import pytest

from tests.golden.equity_bank import parse_equity_bank_statement
from tests.golden.mpesa import parse_mpesa_statement
from tests.golden.ncba_bank import parse_ncba_bank_statement
from app.parsers.profiles.builtin import load_builtin_profiles
from app.parsers.profiles.fingerprint import PageFingerprint, fingerprint_pdf
from app.parsers.profiles.matching import MATCH_THRESHOLD, similarity
from app.parsers.profiles.reader import read_statement
from app.parsers.profiles.schema import validate_profile
from app.services.statement_files import decrypt_if_needed

ROOT = Path(__file__).parent
FIXTURES = ROOT / "fixtures" / "statements"
# The developer's real statements: never committed; passwords only ever
# come from the environment.
SAMPLES = ROOT.parent.parent / "docs" / "sample-statements"

CASES = {
    "mpesa": {
        "institution": ("M-Pesa", "mobile_money"),
        "parser": parse_mpesa_statement,
        "fixture": (FIXTURES / "mpesa" / "statement_sample.pdf", "0000000"),
        "sample": (SAMPLES / "mpesa_statement.pdf", "MPESA_SAMPLE_PASSWORD"),
    },
    "equity_bank": {
        "institution": ("Equity Bank", "bank"),
        "parser": parse_equity_bank_statement,
        "fixture": (FIXTURES / "equity_bank" / "account_statement_sample.pdf", "0000"),
        "sample": (SAMPLES / "equity_statement.pdf", "EQUITY_SAMPLE_PASSWORD"),
    },
    "ncba_bank": {
        "institution": ("NCBA Bank", "bank"),
        "parser": parse_ncba_bank_statement,
        "fixture": (FIXTURES / "ncba_bank" / "e_statement_sample.pdf", "0000000"),
        "sample": (SAMPLES / "ncba_statement.pdf", "NCBA_SAMPLE_PASSWORD"),
    },
}
BUILTIN = {b.key: b for b in load_builtin_profiles()}


def _content(path: Path, password: str) -> bytes:
    return decrypt_if_needed(content=path.read_bytes(), filename=path.name, password=password)


def _assert_golden(key: str, content: bytes):
    builtin = BUILTIN[key]
    expected = CASES[key]["parser"](content)
    result = read_statement(content, validate_profile(builtin.profile), default_currency="KES")
    assert result.check.passed, result.check.message
    assert len(result.transactions) == len(expected)
    for i, (got, want) in enumerate(zip(result.transactions, expected, strict=True)):
        assert got == want, f"row {i + 1} differs"
    assert similarity(fingerprint_pdf(content), PageFingerprint.from_text(builtin.fingerprint)) >= MATCH_THRESHOLD


@pytest.mark.parametrize("key", CASES)
def test_the_builtin_profile_exists_for_its_institution(key):
    builtin = BUILTIN[key]
    validate_profile(builtin.profile)
    assert (builtin.institution_name, builtin.institution_type) == CASES[key]["institution"]


@pytest.mark.parametrize("key", CASES)
def test_the_fixture_reads_exactly_as_the_parser_does(key):
    path, password = CASES[key]["fixture"]
    _assert_golden(key, _content(path, password))


@pytest.mark.parametrize("key", CASES)
def test_the_real_sample_reads_exactly_as_the_parser_does(key):
    path, env = CASES[key]["sample"]
    if not (path.exists() and os.environ.get(env)):
        pytest.skip(f"needs the developer's real sample and {env}")
    _assert_golden(key, _content(path, os.environ[env]))
