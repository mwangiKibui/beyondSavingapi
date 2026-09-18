from datetime import date
from typing import Literal, Protocol, TypedDict


class ParsedTransaction(TypedDict):
    """One row a parser extracts from a statement file.

    Mirrors the transactions table's own columns (docs/schema.sql) so a
    parser's output can be inserted with no further shape translation,
    once ab-44 (write parsed transactions + update import status) exists
    to do that insert.
    """

    txn_date: date
    amount: float
    currency: str
    direction: Literal["in", "out"]
    counterparty: str | None
    description: str | None
    balance_after: float | None
    # Not a transactions column itself - written into the dedupe_hash
    # column by ab-44/ab-43. Only the parser understands its own
    # provider's reference-number format well enough to build a stable
    # fingerprint (see docs/provider-onboarding-runbook.md's per-provider
    # "Dedupe strategy" entries), so it's computed here rather than
    # downstream from generic fields alone.
    dedupe_hash: str


class StatementParser(Protocol):
    """Contract every per-provider parser (ab-39/ab-40/ab-115) must meet.

    Takes the original uploaded file's raw bytes (already password-
    unlocked - see app/services/statement_files.py) and returns every
    transaction it found, oldest first.
    """

    def __call__(self, content: bytes) -> list[ParsedTransaction]: ...


# Keyed by accounts.provider (see app/api/accounts.py's PROVIDERS_BY_TYPE
# for the fixed set of MVP1 providers). Empty until ab-39/ab-40/ab-115
# register a real parser for their provider(s).
PARSERS: dict[str, StatementParser] = {}
