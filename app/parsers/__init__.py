from app.parsers.base import PARSERS
from app.parsers.mpesa import parse_mpesa_statement

# Matches accounts.PROVIDERS_BY_TYPE's mobile_money provider name.
PARSERS["M-Pesa"] = parse_mpesa_statement

__all__ = ["PARSERS"]
