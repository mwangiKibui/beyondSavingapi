"""ab-336: AI category suggestions for business payees nothing else knows.

Two steps (option A of the ab-256 spike, docs/research/do-inference-model-choice.md):

1. One shared answer per business name and direction, picked from the
   default category names (or a new name, or "unknown"), cached for
   everyone in merchant_ai_answers.
2. That answer mapped onto the user's own categories ("Groceries" -> their
   "Grocery"): a local name/synonym match first, and only when that fails a
   small model call with the category name and the user's category names,
   cached per user and category list.

What leaves the app: business names (never a person, number, amount, date,
balance, account or user id) and category names. Nothing here ever blocks
an import or the review: any failure means no AI suggestion.

The lookup runs in the worker after an import is read (and once for an
older import when its review opens); the review reads only the caches.
"""

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from uuid import UUID

from app.core.config import get_settings
from app.core.queues import (
    AI_CATEGORY_JOBS_QUEUE,
    AI_DONE_KEY,
    AI_DONE_TTL_SECONDS,
    AI_PENDING_KEY,
    AI_PENDING_TTL_SECONDS,
)
from app.repositories import ai_categories as repo
from app.services.inference import InferenceError, complete, inference_enabled
from app.services.merchant_keys import RowKey

logger = logging.getLogger(__name__)

SOURCE = "ai"
BATCH_SIZE = 25
CONCURRENT_CALLS = 4
# Names asked about in one job at most; the rest wait for the next upload.
MAX_NAMES_PER_JOB = 500
# The model thinks before it answers: room for that and the JSON.
MAX_TOKENS = 16000

DEFAULT_EXPENSE = [
    "Rent", "Groceries", "Transport", "Airtime", "Utilities", "Dining Out", "Loan Repayment", "Family Support",
    "School Fees", "Medical", "Entertainment", "Subscriptions", "Clothing", "Household", "Bank & mobile charges",
    "Taxes & government fees", "Statutory contributions", "Insurance", "Books & stationery", "Other",
]
DEFAULT_INCOME = [
    "Salary", "Gig Income", "Business Income", "Freelance", "Rental Income", "Gifts Received", "Winnings", "Other",
]
_TYPE_FOR_DIRECTION = {"out": "expense", "in": "income"}


# --- what may be sent --------------------------------------------------------


def ai_name(key: RowKey) -> str | None:
    """The business name the model may see for a row, or None. Only rows the
    key reader is sure are a business; never a person, a reference, a bare
    number, or a row handled at import."""
    if key.is_person or not key.is_business or not key.name:
        return None
    if key.kind not in ("name", "paybill", "till") or key.row_kind or key.is_charge or key.is_loan_repayment:
        return None
    name = " ".join(key.name.upper().split())
    if not re.search(r"[A-Z]{3}", name) or len(name) > 120:
        return None
    return name


def wanted_names(rows: Iterable[dict], keys: dict[UUID, RowKey]) -> list[tuple[str, str]]:
    """(name, direction) for every open row the AI may help with, once each."""
    out: dict[tuple[str, str], None] = {}
    for row in rows:
        name = ai_name(keys[row["id"]])
        if name:
            out[(name, row["direction"])] = None
    return list(out)


# --- step 1: one answer per merchant -----------------------------------------

ANSWER_PROMPT = """You categorise Kenyan bank and M-Pesa payees by their business name only.

Money out categories: {expense}
Money in categories: {income}

Rules:
- Pick the category from the list for the money's direction that fits the business, even if the wording differs.
- Only if none fits, propose a short new category name (1-3 words) and set "new": true.
- If you do not recognise the business or cannot tell what it sells, answer "unknown". Never guess.
- "Other" only when you know the business but nothing else fits.
- A loan paid out, disbursed or refinanced to the account holder is not income: answer "unknown".

Payees:
{payees}

Reply with JSON only: {{"answers": [{{"n": 1, "category": "<name or unknown>", "new": false, \
"confidence": "high|medium|low"}}, ...]}} with one answer per payee, in order."""


def answer_prompt(batch: list[tuple[str, str]]) -> str:
    payees = "\n".join(f'{i}. "{name}" (money {direction})' for i, (name, direction) in enumerate(batch, start=1))
    return ANSWER_PROMPT.format(expense=json.dumps(DEFAULT_EXPENSE), income=json.dumps(DEFAULT_INCOME), payees=payees)


def _json_object(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def parse_answers(text: str, batch: list[tuple[str, str]]) -> list[dict] | None:
    """One answer per payee in the batch (category_name None = unknown), or
    None when the reply isn't the JSON asked for. A payee the reply skips
    gets no answer, so it is asked again next time."""
    data = _json_object(text)
    if data is None or not isinstance(data.get("answers"), list):
        return None
    by_n = {a.get("n"): a for a in data["answers"] if isinstance(a, dict)}
    out = []
    for i, (name, direction) in enumerate(batch, start=1):
        a = by_n.get(i)
        if a is None:
            continue
        category = " ".join(str(a.get("category") or "").split())
        if not category or category.lower() == "unknown" or len(category) > 60 or re.search(r"\d", category):
            category = None
        # Borrowed money is never income (Loans decision): no money-in loan category.
        if category and direction == "in" and "loan" in category.lower():
            category = None
        defaults = DEFAULT_EXPENSE if direction == "out" else DEFAULT_INCOME
        is_new = category is not None and _norm(category) not in {_norm(d) for d in defaults}
        confidence = a.get("confidence") if a.get("confidence") in ("high", "medium", "low") else None
        out.append({"name_key": name, "direction": direction, "category_name": category, "is_new": is_new,
                    "confidence": confidence})
    return out


# --- step 2: onto the user's own categories ----------------------------------

_SYNONYMS = [
    {"groceries", "grocery", "foodstuff", "supermarket"},
    {"transport", "transportation", "travel", "fuel", "petrol", "travelandfuel", "gettingaround", "commute"},
    {"airtime", "airtimeanddata", "phone", "phoneanddata", "data", "mobile", "phonebill"},
    {"utilities", "utility", "bills", "utilitybills"},
    {"diningout", "eatingout", "restaurant", "restaurants", "takeaway"},
    {"medical", "health", "healthcare"},
    {"entertainment", "fun", "leisure"},
    {"schoolfees", "education", "school"},
    {"household", "home", "house"},
    {"clothing", "clothes", "fashion"},
    {"subscription", "subscriptions"},
    {"bankandmobilecharges", "bankcharges", "charges", "fees"},
    {"salary", "wages", "pay"},
]


def _norm(name: str) -> str:
    text = re.sub(r"[^a-z]", "", name.lower().replace("&", "and"))
    return text[:-1] if len(text) > 3 and text.endswith("s") and not text.endswith("ss") else text


def _group(norm: str) -> int | None:
    return next((i for i, g in enumerate(_SYNONYMS) if norm in {_norm(w) for w in g}), None)


def local_match(category_name: str, categories: list[dict]) -> dict | None:
    """The user's category with the same name (ignoring case, plurals and
    "&"), else the only one of theirs in the same synonym group."""
    wanted = _norm(category_name)
    same = [c for c in categories if _norm(c["name"]) == wanted]
    if same:
        return same[0]
    group = _group(wanted)
    if group is None:
        return None
    in_group = [c for c in categories if _group(_norm(c["name"])) == group]
    return in_group[0] if len(in_group) == 1 else None


def categories_hash(categories: list[dict]) -> str:
    """Changes whenever the user adds, renames or removes a category."""
    text = "|".join(sorted(f"{c['type']}:{c['name'].strip().lower()}" for c in categories))
    return hashlib.sha256(text.encode()).hexdigest()[:32]


MAP_PROMPT = """A payee was put under the category name on each line below. For each one, pick the \
category from the user's own list (same money type) that means the same thing, even if worded \
differently, or "none" if none of theirs fits.

User's money out categories: {expense}
User's money in categories: {income}

{items}

Reply with JSON only: {{"answers": [{{"n": 1, "category": "<one of the user's categories or none>"}}, ...]}}"""


def map_prompt(items: list[tuple[str, str]], categories: list[dict]) -> str:
    expense = [c["name"] for c in categories if c["type"] == "expense"]
    income = [c["name"] for c in categories if c["type"] == "income"]
    lines = "\n".join(f'{i}. "{name}" (money {"out" if kind == "expense" else "in"})'
                      for i, (name, kind) in enumerate(items, start=1))
    return MAP_PROMPT.format(expense=json.dumps(expense), income=json.dumps(income), items=lines)


def parse_map(text: str, items: list[tuple[str, str]], categories: list[dict]) -> dict[tuple[str, str], UUID | None]:
    """{(name, type): category id or None}; a reply naming a category the
    user doesn't have of that type counts as no answer."""
    data = _json_object(text)
    if data is None or not isinstance(data.get("answers"), list):
        return {}
    by_n = {a.get("n"): a for a in data["answers"] if isinstance(a, dict)}
    out: dict[tuple[str, str], UUID | None] = {}
    for i, (name, kind) in enumerate(items, start=1):
        a = by_n.get(i)
        if a is None:
            continue
        picked = str(a.get("category") or "").strip()
        if picked.lower() == "none":
            out[(name, kind)] = None
            continue
        match = next((c for c in categories if c["type"] == kind and c["name"].strip().lower() == picked.lower()), None)
        if match:
            out[(name, kind)] = match["id"]
    return out


# --- the review: read the caches ---------------------------------------------


@dataclass
class AiContext:
    answers: dict[tuple[str, str], dict] = field(default_factory=dict)
    user_map: dict[tuple[str, str], UUID | None] = field(default_factory=dict)
    # Wanted names with no fresh answer yet: a lookup is worth queuing.
    missing: int = 0

    def suggestion(self, key: RowKey, direction: str, categories: list[dict]) -> dict | None:
        name = ai_name(key)
        answer = self.answers.get((name, direction)) if name else None
        # "Other" tells the user nothing: no suggestion.
        if not answer or not answer["category_name"] or _norm(answer["category_name"]) == "other":
            return None
        kind = _TYPE_FOR_DIRECTION[direction]
        own = [c for c in categories if c["type"] == kind]
        category = local_match(answer["category_name"], own)
        if category is None and (answer["category_name"], kind) in self.user_map:
            category_id = self.user_map[(answer["category_name"], kind)]
            if category_id is None:
                return _suggested(None, answer["category_name"])
            category = next((c for c in own if c["id"] == category_id), None)
        if category is None:
            return None  # not mapped yet: the lookup job maps it
        if _norm(category["name"]) == "other":
            return None  # mapped onto "Other": still tells the user nothing
        return _suggested(category, category["name"])


def _suggested(category: dict | None, name: str) -> dict:
    return {
        "category_id": category["id"] if category else None,
        "category_name": name,
        "is_new_category": category is None,
        "source": SOURCE,
        "merchant_name": None,
    }


async def load_ai_context(conn, *, user_id: UUID, rows: list[dict], keys: dict, categories: list[dict]) -> AiContext:
    """The cached answers for these rows and the user's mapping of them.
    Never raises: the review opens without AI suggestions instead."""
    try:
        names = wanted_names(rows, keys)
        if not names:
            return AiContext()
        answers = await repo.get_answers(conn, names=names)
        user_map = await repo.get_user_map(conn, user_id=user_id, categories_hash=categories_hash(categories))
        return AiContext(answers=answers, user_map=user_map, missing=sum(n not in answers for n in names))
    except Exception:
        logger.error("Loading AI category answers failed", exc_info=True)
        return AiContext()


# Suggestions the AI's answer replaces when it disagrees: guesses from a
# description keyword. Never the user's own choices (your_rule,
# this_statement), what other users chose, the shared list or own-account
# transfers.
REVIEWED_SOURCES = frozenset({"keyword"})


def with_ai(suggestion: dict | None, ai_suggestion: dict | None) -> dict | None:
    if ai_suggestion is None:
        return suggestion
    if suggestion is None:
        return ai_suggestion
    if suggestion.get("source") in REVIEWED_SOURCES and (
        suggestion.get("kind") == "transfer"
        or suggestion.get("category_name", "").strip().lower() != ai_suggestion["category_name"].strip().lower()
    ):
        return ai_suggestion
    return suggestion


# --- the lookup job ----------------------------------------------------------


async def queue_lookup(import_id: UUID, *, fresh: bool = False) -> bool:
    """Queues the lookup for an import unless one is queued or running, or
    (unless fresh: a new upload) one finished in the last few hours.
    Never raises."""
    if not inference_enabled():
        return False
    try:
        from app.core.redis import get_redis

        redis = get_redis()
        done = AI_DONE_KEY.format(import_id=import_id)
        if fresh:
            await redis.delete(done)
        elif await redis.exists(done):
            return False
        if not await redis.set(AI_PENDING_KEY.format(import_id=import_id), "1", nx=True, ex=AI_PENDING_TTL_SECONDS):
            return False
        await redis.lpush(AI_CATEGORY_JOBS_QUEUE, str(import_id))
        return True
    except Exception:
        logger.error("Queuing the AI category lookup for import %s failed", import_id, exc_info=True)
        return False


async def lookup_pending(import_id: UUID) -> bool:
    if not inference_enabled():
        return False
    try:
        from app.core.redis import get_redis

        return bool(await get_redis().exists(AI_PENDING_KEY.format(import_id=import_id)))
    except Exception:
        return False


async def _clear_pending(import_id: UUID) -> None:
    try:
        from app.core.redis import get_redis

        redis = get_redis()
        await redis.set(AI_DONE_KEY.format(import_id=import_id), "1", ex=AI_DONE_TTL_SECONDS)
        await redis.delete(AI_PENDING_KEY.format(import_id=import_id))
    except Exception:
        logger.warning("Clearing the AI lookup flag for import %s failed", import_id)


async def _ask(prompt: str) -> str | None:
    settings = get_settings()
    try:
        reply = await complete(
            prompt, max_tokens=MAX_TOKENS, model=settings.do_category_model, timeout=settings.do_category_timeout_seconds
        )
    except InferenceError:
        return None
    logger.info("AI category call: %s tokens in, %s out", reply.prompt_tokens, reply.completion_tokens)
    return reply.content


async def answer_names(conn, names: list[tuple[str, str]]) -> int:
    """Step 1 for names with no fresh answer: batches of 25, a few at a
    time. Returns how many answers were saved; a failed batch saves
    nothing, so its names are asked again next time."""
    model = get_settings().do_category_model
    batches = [names[i : i + BATCH_SIZE] for i in range(0, len(names), BATCH_SIZE)]
    gate = asyncio.Semaphore(CONCURRENT_CALLS)
    saved = 0

    async def one(batch: list[tuple[str, str]]) -> None:
        nonlocal saved
        async with gate:
            text = await _ask(answer_prompt(batch))
        answers = parse_answers(text, batch) if text else None
        if answers:
            await repo.save_answers(conn, answers=answers, model=model)
            saved += len(answers)

    await asyncio.gather(*(one(b) for b in batches))
    return saved


async def map_for_user(conn, *, user_id: UUID, categories: list[dict], answers: Iterable[dict]) -> int:
    """Step 2 for answers the local match can't place and the user's
    mapping doesn't hold yet. Only category names are sent."""
    digest = categories_hash(categories)
    known = await repo.get_user_map(conn, user_id=user_id, categories_hash=digest)
    items: dict[tuple[str, str], None] = {}
    for a in answers:
        if not a["category_name"]:
            continue
        kind = _TYPE_FOR_DIRECTION[a["direction"]]
        own = [c for c in categories if c["type"] == kind]
        if (a["category_name"], kind) in known or local_match(a["category_name"], own):
            continue
        items[(a["category_name"], kind)] = None
    todo = list(items)
    if not todo:
        return 0
    if not categories:
        mapping: dict = dict.fromkeys(todo)
    else:
        text = await _ask(map_prompt(todo, categories))
        mapping = parse_map(text, todo, categories) if text else {}
    await repo.save_user_map(conn, user_id=user_id, categories_hash=digest, mapping=mapping)
    return len(mapping)
