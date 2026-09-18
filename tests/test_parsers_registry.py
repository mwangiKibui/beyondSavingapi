from app.parsers import PARSERS
from app.parsers.mpesa import parse_mpesa_statement


def test_mpesa_is_registered():
    assert PARSERS["M-Pesa"] is parse_mpesa_statement
