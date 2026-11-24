from app.parsers.base import PARSERS
from app.parsers.equity_bank import parse_equity_bank_statement
from app.parsers.mpesa import parse_mpesa_statement
from app.parsers.ncba_bank import parse_ncba_bank_statement

# Keyed by the institution's name, as accounts.provider stores it.
PARSERS["M-Pesa"] = parse_mpesa_statement
PARSERS["Equity Bank"] = parse_equity_bank_statement
PARSERS["NCBA Bank"] = parse_ncba_bank_statement

# Mentor Sacco member statements are read by their own path in the worker
# (sub-ledgers), not through PARSERS.
MENTOR_SACCO = "Mentor Sacco"

# Every provider the app can read statements for - institutions may only be
# marked statements_uploadable when they are in here (ab-197).
STATEMENT_PROVIDERS = frozenset(PARSERS) | {MENTOR_SACCO}

__all__ = ["MENTOR_SACCO", "PARSERS", "STATEMENT_PROVIDERS"]
