"""What a SACCO sub-ledger holds (ab-259) - decides whether its rows are
spending, saving or a loan. Mirrors beyondSavingdb migration 000026's
backfill so a new sub-ledger gets the same default an old one did."""

import re

ROLES = ("spending", "locked_saving", "shares", "loan")
# Rows on these are the member's own savings: never income or spending.
SAVING_ROLES = frozenset({"locked_saving", "shares"})


def infer_role(name: str, balance_treatment: str) -> str:
    n = name.lower()
    if balance_treatment == "deduction" or "loan" in n:
        return "loan"
    if "share" in n:
        return "shares"
    if re.search(r"non.*withdrawable", n) or "ordinary deposit" in n or ("deposit" in n and "saving" not in n):
        return "locked_saving"
    return "spending"
