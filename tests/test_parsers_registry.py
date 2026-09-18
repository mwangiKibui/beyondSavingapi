from app.parsers import PARSERS
from app.parsers.equity_bank import parse_equity_bank_statement
from app.parsers.mpesa import parse_mpesa_statement
from app.parsers.ncba_bank import parse_ncba_bank_statement


def test_mpesa_is_registered():
    assert PARSERS["M-Pesa"] is parse_mpesa_statement


def test_equity_bank_is_registered():
    assert PARSERS["Equity Bank"] is parse_equity_bank_statement


def test_ncba_bank_is_registered():
    assert PARSERS["NCBA Bank"] is parse_ncba_bank_statement
