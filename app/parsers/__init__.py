from app.parsers.base import PARSERS

# M-Pesa, Equity Bank and NCBA Bank are read by built-in statement profiles
# (app/parsers/profiles/builtin, ab-313/ab-320); their old parsers live on
# in tests/golden as references only. PARSERS stays for any provider that
# still needs its own code.

# Mentor Sacco member statements are read by their own path in the worker
# (sub-ledgers), not through PARSERS.
MENTOR_SACCO = "Mentor Sacco"

# Every provider the app can read statements for - institutions may only be
# marked statements_uploadable when they are in here (ab-197).
STATEMENT_PROVIDERS = frozenset({"M-Pesa", "Equity Bank", "NCBA Bank", MENTOR_SACCO}) | frozenset(PARSERS)

__all__ = ["MENTOR_SACCO", "PARSERS", "STATEMENT_PROVIDERS"]
