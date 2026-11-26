"""ab-336: the worker's AI category lookup for one import (see
app/services/ai_categories.py). Runs after the import is read, or once for
an older import when its review opens. Never raises."""

import logging
from uuid import UUID

from app.core.config import get_settings
from app.repositories import ai_categories as repo
from app.repositories.category_suggestions import get_import_rows, get_learned_names, get_user_categories
from app.repositories.transfer_reasons import get_user_transfer_reasons
from app.services import ai_categories
from app.services.category_suggestions import apply_learned_names, extract_keys, learned_name_lookups
from app.services.directory_cache import get_directory

logger = logging.getLogger(__name__)


async def run_lookup(pool, import_id: UUID) -> None:
    outcome = "failed"
    try:
        outcome = await _run(pool, import_id)
    except Exception:
        logger.error("AI category lookup failed for import %s", import_id, exc_info=True)
    finally:
        # ab-339: a lookup that missed some answers may run again soon.
        await ai_categories._clear_pending(import_id, failed=outcome == "failed", retry_soon=outcome != "done")


async def _run(pool, import_id: UUID) -> str:
    """"done"; "partial" when some batches got no answer; "failed" when it
    asked and got no answers at all (ab-338: Processing and Allocation then
    say suggestions are unavailable)."""
    record = await repo.get_import_for_lookup(pool, import_id=import_id)
    if record is None:
        return "done"
    rows = await get_import_rows(
        pool,
        import_id=import_id,
        account_id=record["account_id"],
        period_start=record["period_start"],
        period_end=record["period_end"],
    )
    # The same keys the review builds, so it finds these answers.
    keys = extract_keys(rows, provider=record["provider"], secret=get_settings().suggestion_key_secret)
    directory = await get_directory(pool)
    learned = await get_learned_names(pool, numbers=learned_name_lookups(rows, keys, directory))
    keys = apply_learned_names(rows, keys, learned, directory)
    names = ai_categories.wanted_names(rows, keys)
    texts = ai_categories.wanted_texts(rows, keys)  # ab-339: masked descriptions
    if not names and not texts:
        return "done"
    user_id = record["user_id"]
    known = await repo.get_answers(pool, names=names)
    missing = [n for n in names if n not in known][: ai_categories.MAX_NAMES_PER_JOB]
    known_texts = await repo.get_text_answers(pool, user_id=user_id, texts=texts)
    missing_texts = [t for t in texts if t not in known_texts][: ai_categories.MAX_NAMES_PER_JOB]
    await ai_categories.set_lookup_payees(import_id, len(missing) + len(missing_texts))
    categories = await get_user_categories(pool, user_id=user_id)
    saved = await ai_categories.answer_names(pool, missing) if missing else 0
    saved_texts = 0
    if missing_texts:
        reasons = await get_user_transfer_reasons(pool, user_id=user_id)
        saved_texts = await ai_categories.answer_texts(
            pool, user_id=user_id, texts=missing_texts, categories=categories, reasons=reasons
        )
    mapped = 0
    if names:
        answers = await repo.get_answers(pool, names=names)
        mapped = await ai_categories.map_for_user(
            pool, user_id=user_id, categories=categories, answers=answers.values()
        )
    logger.info(
        "Import %s: AI lookup for %d business payees and %d descriptions - %d asked, %d answered, "
        "%d categories mapped",
        import_id, len(names), len(texts), len(missing) + len(missing_texts), saved + saved_texts, mapped,
    )
    asked, answered = len(missing) + len(missing_texts), saved + saved_texts
    if asked and answered == 0:
        return "failed"
    return "partial" if answered < asked else "done"
