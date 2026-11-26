import asyncio
import logging
from datetime import date
from uuid import UUID

import asyncpg

from app.core.config import get_settings
from app.core.queues import AI_CATEGORY_JOBS_QUEUE, PARSE_JOBS_QUEUE
from app.core.redis import get_redis
from app.core.storage import get_storage_client
from app.parsers import MENTOR_SACCO, PARSERS
from app.parsers.base import ParsedTransaction, StatementParseError, derive_opening_balance, statement_period
from app.parsers.mentor_sacco import SubLedgerStatement, parse_mentor_sacco_statement
from app.repositories.statement_imports import (
    get_import_for_processing,
    get_import_sub_ledgers,
    mark_import_failed,
    mark_import_parsed,
    set_import_fingerprint,
    return_to_mapping,
    set_import_status,
    set_sub_ledger_opening_balances,
)
from app.repositories.notifications import notify_statement_ready
from app.repositories.statement_profiles import get_profile, list_active_profiles, record_check_passed
from app.repositories.transactions import insert_transactions
from app.services.ai_categories import begin_lookup, cancel_lookup, queue_lookup
from app.services.ai_category_job import run_lookup
from app.services.import_handling import handle_import_by_id
from app.services.statement_files import file_type_of
from app.services.layout_proposer import propose_layout
from app.services.statement_profiles import ProfileRead, read_with_chosen_profile, read_with_profiles

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# How long BRPOP blocks before giving the loop a chance to check for
# cancellation - not a retry/backoff interval, just a liveness poll.
POLL_TIMEOUT_SECONDS = 5

# Special-cased ahead of the PARSERS registry lookup (app/parsers/__init__.py
# deliberately leaves it unregistered - see ab-115/ab-116): its parser
# returns list[SubLedgerStatement], one entry per sub-ledger *instance*,
# not the flat list[ParsedTransaction] every other provider's parser
# returns, so it needs its own write path below. The name lives in
# app.parsers so the institutions API knows it has a statement reader too.


class ImportWriteError(Exception):
    """The parser succeeded, but this import can't be safely written -
    e.g. no sub-ledgers were selected, or a selected sub-ledger's name
    has no matching section in the parsed statement. Raised instead of
    a partial/wrong write, so the import fails clearly with a specific
    reason rather than silently dropping or mis-filing transactions.
    """


async def _write_single_account_import(
    pool: asyncpg.Pool,
    *,
    import_id: UUID,
    account_id: UUID,
    transactions: list[ParsedTransaction],
    profile_id: UUID | None = None,
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
    opening_balance, opening_balance_date = derive_opening_balance(transactions)
    # profile_id only when a stored profile read the file, so the bespoke
    # parsers' writes are exactly what they were before ab-312.
    extra = {"profile_id": profile_id} if profile_id is not None else {}
    await mark_import_parsed(
        pool,
        import_id=import_id,
        period_start=period_start,
        period_end=period_end,
        row_count=len(transactions),
        opening_balance=opening_balance,
        opening_balance_date=opening_balance_date,
        **extra,
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
    opening_balances: list[tuple[UUID, float, date]] = []
    for sub_ledger_id, transactions in resolved:
        total_inserted += await insert_transactions(
            pool,
            account_id=account_id,
            import_id=import_id,
            transactions=transactions,
            sub_ledger_id=sub_ledger_id,
        )
        all_transactions.extend(transactions)

        # Each sub-ledger is its own independent balance sequence (see
        # SubLedgerStatement), so its opening balance is derived from its
        # OWN oldest transaction, not the combined list below.
        opening_balance, opening_balance_date = derive_opening_balance(transactions)
        if opening_balance is not None and opening_balance_date is not None:
            opening_balances.append((sub_ledger_id, opening_balance, opening_balance_date))

    skipped = len(all_transactions) - total_inserted
    if skipped:
        logger.info(
            "Import %s: skipped %d duplicate transaction(s) across selected sub-ledgers",
            import_id,
            skipped,
        )

    if opening_balances:
        await set_sub_ledger_opening_balances(pool, import_id=import_id, opening_balances=opening_balances)

    period_start, period_end = statement_period(all_transactions) if all_transactions else (None, None)
    await mark_import_parsed(
        pool,
        import_id=import_id,
        period_start=period_start,
        period_end=period_end,
        row_count=len(all_transactions),
    )


async def _handle_rows(pool: asyncpg.Pool, *, import_id: UUID) -> None:
    # ab-281: charges, loans and own-account transfers leave the review.
    # A failure here never fails the import - handled_at stays NULL and the
    # handling runs again when the review opens.
    try:
        result = await handle_import_by_id(pool, import_id=import_id, secret=get_settings().suggestion_key_secret)
        if result is not None:
            logger.info("Import %s: handled %s, paired %d transfer(s)", import_id, dict(result.counts), result.paired)
    except Exception:
        logger.error("Handling rows failed for import %s - will retry when its review opens", import_id, exc_info=True)


async def _load_profiles(pool: asyncpg.Pool, record: dict) -> list[dict]:
    """ab-312: active profiles, when the account's provider is a known
    institution. A failure here never blocks the bespoke parser."""
    if record.get("institution_id") is None:
        return []
    try:
        return await list_active_profiles(pool)
    except Exception:
        logger.error("Loading statement profiles failed for import %s", record["id"], exc_info=True)
        return []


async def _chosen_profile(pool: asyncpg.Pool, record: dict) -> dict | None:
    """The layout the uploader saved for this upload, while it waits to be
    read with it (a pending upload with a profile_id)."""
    if record.get("status") != "pending" or record.get("profile_id") is None:
        return None
    try:
        return await get_profile(pool, profile_id=record["profile_id"])
    except Exception:
        logger.error("Loading the saved layout failed for import %s", record["id"], exc_info=True)
        return None


def _can_be_mapped(record: dict, attempt: ProfileRead) -> bool:
    """ab-315: the wizard can map a file with text we could fingerprint, for
    an institution layouts can be saved for, that isn't another
    institution's statement."""
    return (
        attempt.fingerprint is not None
        and attempt.wrong_institution is None
        and record.get("institution_id") is not None
    )


async def _tell_uploader(pool: asyncpg.Pool, record: dict) -> None:
    try:
        await notify_statement_ready(pool, user_id=record["user_id"], import_id=record["id"], provider=record["provider"])
    except Exception:
        logger.error("Telling the uploader import %s is ready failed", record["id"], exc_info=True)


def _no_read_detail(record: dict, attempt: ProfileRead, file_type: str = "pdf") -> str:
    if attempt.wrong_institution:
        return (
            f"This looks like a {attempt.wrong_institution} statement. "
            "Check you uploaded it to the right account."
        )
    if attempt.failure:
        return attempt.failure
    if record.get("provider") == MENTOR_SACCO:
        return "Mentor Sacco statements can only be read as PDF files"
    if attempt.fingerprint is None:
        return "We couldn't find any text in this file. Download your statement again as a PDF, CSV or Excel file."
    return "We can't read this file's layout yet"


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
        # ab-319: the bespoke parsers only read PDFs; CSV/XLSX only profiles.
        file_type = file_type_of(record.get("file_name") or "") or "pdf"
        if file_type != "pdf":
            parser = None
        profiles = [] if is_mentor_sacco else await _load_profiles(pool, record)
        # ab-315: an upload waiting for support is re-read when a layout for
        # its institution appears; it keeps waiting unless the read works.
        was_waiting = record.get("status") == "awaiting_support"

        settings = get_settings()
        content = (
            get_storage_client()
            .get_object(Bucket=settings.storage_bucket, Key=record["storage_key"])["Body"]
            .read()
        )
        attempt = ProfileRead()
        chosen = await _chosen_profile(pool, record) if not is_mentor_sacco else None
        if chosen is not None:
            # The uploader just saved this layout for this upload (and it
            # passed the check on this file): read with it, no matching.
            attempt = read_with_chosen_profile(content, row=chosen, currency=record.get("currency") or "KES")
            if attempt.transactions is None:
                detail = f"We couldn't read the whole statement with these columns: {attempt.failure}"
                logger.warning("Import %s: saved layout %s didn't read it (%s)", import_id, chosen["id"], attempt.failure)
                await return_to_mapping(pool, import_id=parsed_import_id, error_detail=detail)
                return
        elif not is_mentor_sacco:
            # ab-312: fingerprint every upload; read it with a stored profile
            # when one of this institution's matches and passes its check.
            attempt = read_with_profiles(
                content,
                file_name=record.get("file_name") or "statement.pdf",
                institution_id=record.get("institution_id"),
                currency=record.get("currency") or "KES",
                candidates=profiles,
            )
            if attempt.fingerprint is not None:
                try:
                    await set_import_fingerprint(pool, import_id=parsed_import_id, fingerprint=attempt.fingerprint)
                except Exception:
                    logger.error("Storing the fingerprint failed for import %s", import_id, exc_info=True)
            if attempt.wrong_institution:
                logger.warning(
                    "Import %s (provider %s) looks like a %s statement",
                    import_id,
                    record["provider"],
                    attempt.wrong_institution,
                )
            if attempt.transactions is None and attempt.matched_profile_id is not None:
                logger.warning(
                    "Import %s: profile %s didn't read it (%s) - falling back to the %s",
                    import_id,
                    attempt.matched_profile_id,
                    attempt.failure,
                    "bespoke parser" if parser else "mapping wizard",
                )

        # ab-338: flagged before the upload can be marked parsed, so the
        # wizard never sees it parsed without its AI lookup running.
        lookup_begun = await begin_lookup(parsed_import_id)
        lookup_queued = False
        try:
            if attempt.transactions is not None:
                await _write_single_account_import(
                    pool,
                    import_id=parsed_import_id,
                    account_id=record["account_id"],
                    transactions=attempt.transactions,
                    profile_id=attempt.profile_id,
                )
                await record_check_passed(pool, profile_id=attempt.profile_id)
                if was_waiting:
                    await _tell_uploader(pool, record)
                logger.info(
                    "Import %s (provider %s) read by profile %s v%s (%d rows)",
                    import_id,
                    record["provider"],
                    attempt.profile_id,
                    attempt.profile_version,
                    len(attempt.transactions),
                )
            elif parser is None:
                if was_waiting:
                    logger.info("Import %s (provider %s) still waiting for a layout", import_id, record["provider"])
                    return
                if _can_be_mapped(record, attempt):
                    # ab-315: no stored layout reads it - the uploader maps it.
                    logger.info("Import %s (provider %s) needs mapping", import_id, record["provider"])
                    # ab-314: a masked page to the AI first, when our own guess
                    # doesn't pass; a passing proposal opens Columns pre-filled.
                    await propose_layout(pool, record=record, content=content, file_type=file_type)
                    await set_import_status(pool, import_id=parsed_import_id, status="needs_mapping")
                    return
                detail = _no_read_detail(record, attempt, file_type)
                logger.info("Import %s (provider %s) not read: %s", import_id, record["provider"], detail)
                await mark_import_failed(pool, import_id=parsed_import_id, error_detail=detail)
                return
            elif is_mentor_sacco:
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
            await _handle_rows(pool, import_id=parsed_import_id)
            # ab-336: AI categories for its business payees, in the background.
            lookup_queued = await queue_lookup(parsed_import_id, fresh=True, begun=lookup_begun)
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
        finally:
            if lookup_begun and not lookup_queued:
                await cancel_lookup(parsed_import_id)
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


# ab-336: AI lookups wait on the model for tens of seconds, so they run as
# tasks (a few at once) while the loop keeps taking parse jobs.
_AI_LOOKUPS_AT_ONCE = 2


async def _ai_lookup(pool: asyncpg.Pool, gate: asyncio.Semaphore, import_id: str) -> None:
    async with gate:
        try:
            await run_lookup(pool, UUID(import_id))
        except ValueError:
            logger.warning("Ignoring an AI lookup job with a bad import id")


async def run_worker(pool: asyncpg.Pool) -> None:
    redis_client = get_redis()
    logger.info("Worker started, watching '%s' and '%s'", PARSE_JOBS_QUEUE, AI_CATEGORY_JOBS_QUEUE)
    gate = asyncio.Semaphore(_AI_LOOKUPS_AT_ONCE)
    running: set[asyncio.Task] = set()
    while True:
        popped = await redis_client.brpop([PARSE_JOBS_QUEUE, AI_CATEGORY_JOBS_QUEUE], timeout=POLL_TIMEOUT_SECONDS)
        if popped is None:
            continue
        queue, import_id = popped
        if queue == AI_CATEGORY_JOBS_QUEUE:
            task = asyncio.create_task(_ai_lookup(pool, gate, import_id))
            running.add(task)
            task.add_done_callback(running.discard)
            continue
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
