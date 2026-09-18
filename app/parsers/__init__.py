from app.parsers.base import PARSERS
from app.parsers.equity_bank import parse_equity_bank_statement
from app.parsers.mpesa import parse_mpesa_statement
from app.parsers.ncba_bank import parse_ncba_bank_statement

# Matches accounts.PROVIDERS_BY_TYPE's provider names.
PARSERS["M-Pesa"] = parse_mpesa_statement
PARSERS["Equity Bank"] = parse_equity_bank_statement
PARSERS["NCBA Bank"] = parse_ncba_bank_statement

__all__ = ["PARSERS"]
