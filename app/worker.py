import asyncio
import logging
from uuid import UUID

import asyncpg

from app.core.config import get_settings
from app.core.queues import PARSE_JOBS_QUEUE
from app.core.redis import get_redis
from app.core.storage import get_storage_client
from app.parsers import PARSERS
from app.parsers.base import ParsedTransaction, StatementParseError, statement_period
from app.parsers.mentor_sacco import SubLedgerStatement, parse_mentor_sacco_statement
from app.repositories.statement_imports import (
    get_import_for_processing,
    get_import_sub_ledgers,
    mark_import_failed,
    mark_import_parsed,
)
from app.repositories.transactions import insert_transactions

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# How long BRPOP blocks before giving the loop a chance to check for
# cancellation - not a retry/backoff interval, just a liveness poll.
POLL_TIMEOUT_SECONDS = 5

# Special-cased ahead of the PARSERS registry lookup (app/parsers/__init__.py
# deliberately leaves it unregistered - see ab-115/ab-116): its parser
# returns list[SubLedgerStatement], one entry per sub-ledger *instance*,
# not the flat list[ParsedTransaction] every other provider's parser
# returns, so it needs its own write path below.
MENTOR_SACCO = "Mentor Sacco"


class ImportWriteError(Exception):
    """The parser succeeded, but this import can't be safely written -
    e.g. no sub-ledgers were selected, or a selected sub-ledger's name
    has no matching section in the parsed statement. Raised instead of
    a partial/wrong write, so the import fails clearly with a specific
    reason rather than silently dropping or mis-filing transactions.
    """


async def _write_single_account_import(
    pool: asyncpg.Pool, *, import_id: UUID, account_id: UUID, transactions: list[ParsedTransaction]
) -> None:
    inserted = await insert_transactions(
        pool, account_id=account_id, import_id=import_id, transactions=transactions
    )
    skipped = len(transactions) - inserted
    if skipped:
        logger.info(
            "Import %s: skipped %d duplicate transaction(s) already on this account", import_id, skipped
        )

    period_start, period_end = statement_period(transactions) if transactions else (None, None)
    await mark_import_parsed(
        pool,
        import_id=import_id,
        period_start=period_start,
        period_end=period_end,
        row_count=len(transactions),
    )


async def _write_sub_ledger_import(
    pool: asyncpg.Pool,
    *,
    import_id: UUID,
    account_id: UUID,
    sections: list[SubLedgerStatement],
) -> None:
    # Which of the account's sub-ledgers the uploader actually selected
    # for this file (ab-119) - persisted at upload time, since the
    # worker picks the job up later in a separate process. A section in
    # the parsed statement that wasn't selected is ignored entirely,
    # even though it's present in the file (developer's explicit call).
    selected = await get_import_sub_ledgers(pool, import_id=import_id)
    if not selected:
        raise ImportWriteError(
            "No sub-ledgers were selected for this upload - choose at least one to import."
        )

    resolved: list[tuple[UUID, list[ParsedTransaction]]] = []
    for sub_ledger in selected:
        # "Instant Loan" can appear twice as two genuinely separate
        # balance sequences (see ab-115) but there's only ever one
        # "Instant Loan" sub-ledger - every matching section's
        # transactions insert against that one sub_ledger_id.
        matching_sections = [s for s in sections if s["name"] == sub_ledger["name"]]
        if not matching_sections:
            raise ImportWriteError(
                f"No section named '{sub_ledger['name']}' was found in this statement - "
                "check the sub-ledger's name matches the statement exactly."
            )
        transactions = [txn for section in matching_sections for txn in section["transactions"]]
        resolved.append((sub_ledger["id"], transactions))

    all_transactions: list[ParsedTransaction] = []
    total_inserted = 0
    for sub_ledger_id, transactions in resolved:
        total_inserted += await insert_transactions(
            pool,
            account_id=account_id,
            import_id=import_id,
            transactions=transactions,
            sub_ledger_id=sub_ledger_id,
        )
        all_transactions.extend(transactions)

    skipped = len(all_transactions) - total_inserted
    if skipped:
        logger.info(
            "Import %s: skipped %d duplicate transaction(s) across selected sub-ledgers",
            import_id,
            skipped,
        )

    period_start, period_end = statement_period(all_transactions) if all_transactions else (None, None)
    await mark_import_parsed(
        pool,
        import_id=import_id,
        period_start=period_start,
        period_end=period_end,
        row_count=len(all_transactions),
    )


async def process_job(pool: asyncpg.Pool, import_id: str) -> None:
    try:
        parsed_import_id = UUID(import_id)
    except ValueError:
        logger.error("Parse job had a malformed import id: %r", import_id)
        return

    try:
        record = await get_import_for_processing(pool, import_id=parsed_import_id)
        if record is None:
            # The import row is gone (e.g. its account was deleted) -
            # nothing to process, and nothing to mark failed either.
            logger.warning("Parse job for unknown import %s - skipping", import_id)
            return

        is_mentor_sacco = record["provider"] == MENTOR_SACCO
        parser = parse_mentor_sacco_statement if is_mentor_sacco else PARSERS.get(record["provider"])
        if parser is None:
            logger.info(
                "No parser registered for provider %s (import %s) - marking failed",
                record["provider"],
                import_id,
            )
            await mark_import_failed(
                pool,
                import_id=parsed_import_id,
                error_detail=f"No parser available yet for {record['provider']}",
            )
            return

        settings = get_settings()
        content = (
            get_storage_client()
            .get_object(Bucket=settings.minio_bucket, Key=record["storage_key"])["Body"]
            .read()
        )
        try:
            if is_mentor_sacco:
                sections = parser(content)
                await _write_sub_ledger_import(
                    pool,
                    import_id=parsed_import_id,
                    account_id=record["account_id"],
                    sections=sections,
                )
            else:
                transactions = parser(content)
                await _write_single_account_import(
                    pool,
                    import_id=parsed_import_id,
                    account_id=record["account_id"],
                    transactions=transactions,
                )
        except (StatementParseError, ImportWriteError) as e:
            # A parser's own validation (ab-42's validate_running_balance,
            # a missing required anchor like Equity Bank/NCBA's opening
            # balance, or - for Mentor Sacco - a missing sub-ledger
            # account) rejected this import - a clear, non-sensitive
            # reason to show the user, not the generic fallback below.
            logger.info(
                "Import %s (provider %s) rejected: %s", import_id, record["provider"], e
            )
            await mark_import_failed(pool, import_id=parsed_import_id, error_detail=str(e))
    except Exception:
        logger.error("Unexpected error processing parse job %s", import_id, exc_info=True)
        try:
            await mark_import_failed(
                pool,
                import_id=parsed_import_id,
                error_detail="Something went wrong while processing this statement.",
            )
        except Exception:
            logger.error("Additionally failed to mark import %s as failed", import_id, exc_info=True)


async def run_worker(pool: asyncpg.Pool) -> None:
    redis_client = get_redis()
    logger.info("Parse-job worker started, watching '%s'", PARSE_JOBS_QUEUE)
    while True:
        popped = await redis_client.brpop(PARSE_JOBS_QUEUE, timeout=POLL_TIMEOUT_SECONDS)
        if popped is None:
            continue
        _, import_id = popped
        await process_job(pool, import_id)


async def main() -> None:
    settings = get_settings()
    pool = await asyncpg.create_pool(settings.database_url)
    try:
        await run_worker(pool)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
