import asyncio
import logging
from uuid import UUID

import asyncpg

from app.core.config import get_settings
from app.core.queues import PARSE_JOBS_QUEUE
from app.core.redis import get_redis
from app.core.storage import get_storage_client
from app.parsers.base import PARSERS
from app.repositories.statement_imports import get_import_for_processing, mark_import_failed

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# How long BRPOP blocks before giving the loop a chance to check for
# cancellation - not a retry/backoff interval, just a liveness poll.
POLL_TIMEOUT_SECONDS = 5


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

        parser = PARSERS.get(record["provider"])
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
        parser(content)
        # ab-44 (write parsed transactions + update import status to
        # "parsed") owns turning the parser's output into rows - nothing
        # further to do here once a provider actually has one.
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
