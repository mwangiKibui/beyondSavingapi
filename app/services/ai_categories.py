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

ab-339: every other open row is asked about by its description with its
identifiers masked (mask_description: numbers, references, codes and dates
become ***; words, names included, are kept - developer decision
2026-11-26). The model picks one of the user's own categories, one of
their transfer reasons (a Funds transfer), or unknown. Those answers are
private to the user (user_ai_text_answers), never shared.

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
from app.repositories.transfer_reasons import get_user_transfer_reasons
from app.services.inference import EmptyReply, InferenceError, complete, inference_enabled
from app.services.merchant_keys import RowKey

logger = logging.getLogger(__name__)

SOURCE = "ai"
BATCH_SIZE = 25
# ab-339: descriptions are messier than business names (name-only rows the
# model mulls over), so fewer per call keeps its thinking - and replies -
# short and predictable.
TEXT_BATCH_SIZE = 15
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


# --- ab-339: masked descriptions --------------------------------------------

_MAX_TEXT = 300
# Separators some banks pack a row's fields with.
_PACKED_SEPARATORS = re.compile(r"[~|_]+")
_DATE = re.compile(r"(?<![^\W_])\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}(?![^\W_])")
# Any run of letters/digits with a digit in it: phone, account and card
# numbers, references, transaction codes.
_IDENTIFIER = re.compile(r"[^\W_]*\d[^\W_]*")
_STARS = re.compile(r"\*+")
_STAR_RUN = re.compile(r"\*\*\*(?:[\s:.,;/#>-]*\*\*\*)+")
_WORD = re.compile(r"[^\W\d_]{3,}")


def mask_description(description: str | None) -> str | None:
    """The description with its identifiers masked (***), upper-cased; None
    when no word of 3+ letters is left to ask about. Names are kept."""
    text = _PACKED_SEPARATORS.sub(" ", (description or "").upper())
    text = _DATE.sub("***", text)
    text = _IDENTIFIER.sub("***", text)
    text = _STARS.sub("***", text)
    text = _STAR_RUN.sub("***", text)
    text = " ".join(text.split())
    if not _WORD.search(text):
        return None
    if len(text) > _MAX_TEXT:
        text = text[:_MAX_TEXT].rsplit(" ", 1)[0]
    return text


def _askable(key: RowKey) -> bool:
    """Rows handled at import are never asked about. ab-345: nor is a
    person whose row shows their number but no name (Co-op "TRANSFER TO
    M-PESA 2547…"): masked, every such row reads the same, so one answer
    would land on everyone. Those learn from the user's own choices."""
    if key.is_person and key.person_number and not key.person_name:
        return False
    return not (key.row_kind or key.is_charge or key.is_loan_repayment)


def wanted_texts(rows: Iterable[dict], keys: dict[UUID, RowKey]) -> list[tuple[str, str]]:
    """(masked text, direction) for every open row that isn't a business
    name (those are asked about by name) and wasn't handled at import."""
    out: dict[tuple[str, str], None] = {}
    for row in rows:
        key = keys[row["id"]]
        if row.get("allocated") or row.get("is_transfer") or ai_name(key) or not _askable(key):
            continue
        text = mask_description(row.get("description"))
        if text:
            out[(text, row["direction"])] = None
    return list(out)


def _reasons_for(reasons: list[dict], direction: str) -> list[dict]:
    return [r for r in reasons if r.get("direction") in (None, direction)]


TEXT_PROMPT = """You help sort rows of a Kenyan bank or M-Pesa statement. Each row is the \
statement's description with numbers, references and dates replaced by ***.

The account holder's categories:
Money out: {expense}
Money in: {income}

Their funds-transfer reasons (money moved between their own accounts, savings or investments, \
or to and from family, that is not spending or income):
Money out: {reasons_out}
Money in: {reasons_in}

For each row answer one of:
- "category": the category for the money's direction that fits, even if the wording differs. \
Only if none fits, a short new category name (1-3 words) with "new": true.
- "transfer": a funds transfer; "name" must be one of the transfer reasons for that direction.
- "unknown": when you cannot tell what the row is. Never guess; a person's name alone is usually unknown.

Rules:
- Never answer "Other".
- A loan paid out, disbursed or refinanced to the account holder is not income: answer unknown.
- Interest charged is an expense; interest earned is income.

Rows:
{rows}

Reply with JSON only: {{"answers": [{{"n": 1, "type": "category|transfer|unknown", "name": "<name>", \
"new": false}}, ...]}} with one answer per row, in order."""


def text_prompt(batch: list[tuple[str, str]], categories: list[dict], reasons: list[dict]) -> str:
    def names(items):
        return json.dumps([i["name"] for i in items if _norm(i["name"]) != "other"])

    rows = "\n".join(f'{i}. "{text}" (money {direction})' for i, (text, direction) in enumerate(batch, start=1))
    return TEXT_PROMPT.format(
        expense=names([c for c in categories if c["type"] == "expense"]),
        income=names([c for c in categories if c["type"] == "income"]),
        reasons_out=names(_reasons_for(reasons, "out")),
        reasons_in=names(_reasons_for(reasons, "in")),
        rows=rows,
    )


def _text_answer(a: dict, direction: str, categories: list[dict], reasons: list[dict]) -> tuple[str, str | None, bool]:
    name = " ".join(str(a.get("name") or "").split())
    if not name or len(name) > 60 or re.search(r"\d", name) or _norm(name) in ("other", "unknown"):
        return "unknown", None, False
    if a.get("type") == "transfer":
        reason = next((r for r in _reasons_for(reasons, direction) if _norm(r["name"]) == _norm(name)), None)
        return ("transfer", reason["name"], False) if reason else ("unknown", None, False)
    if a.get("type") != "category":
        return "unknown", None, False
    # Borrowed money is never income (Loans decision): no money-in loan category.
    if direction == "in" and "loan" in name.lower():
        return "unknown", None, False
    own = local_match(name, [c for c in categories if c["type"] == _TYPE_FOR_DIRECTION[direction]])
    if own is not None:
        return ("unknown", None, False) if _norm(own["name"]) == "other" else ("category", own["name"], False)
    return "category", name, True


def parse_text_answers(
    text: str, batch: list[tuple[str, str]], categories: list[dict], reasons: list[dict]
) -> list[dict] | None:
    """One answer per row of the batch, or None when the reply isn't the
    JSON asked for. A row the reply skips is asked again next time."""
    data = _json_object(text)
    if data is None or not isinstance(data.get("answers"), list):
        return None
    by_n = {a.get("n"): a for a in data["answers"] if isinstance(a, dict)}
    out = []
    for i, (text_key, direction) in enumerate(batch, start=1):
        if i not in by_n:
            continue
        answer_type, name, is_new = _text_answer(by_n[i], direction, categories, reasons)
        out.append({"text_key": text_key, "direction": direction, "answer_type": answer_type, "name": name,
                    "is_new": is_new})
    return out


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
    # Wanted names/texts with no fresh answer yet: a lookup is worth queuing.
    missing: int = 0
    # ab-345: AI is configured, so a missing answer is worth asking again.
    enabled: bool = False
    # ab-339: this user's answers for masked descriptions, and their
    # transfer reasons (for a Funds transfer answer).
    text_answers: dict[tuple[str, str], dict] = field(default_factory=dict)
    reasons: list[dict] = field(default_factory=list)

    def suggestion(
        self, key: RowKey, direction: str, categories: list[dict], description: str | None = None
    ) -> dict | None:
        name = ai_name(key)
        if name is None:
            return self._text_suggestion(key, direction, categories, description)
        answer = self.answers.get((name, direction))
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

    def unanswered(self, key: RowKey, direction: str, description: str | None) -> bool:
        """ab-345: the AI was meant to answer this row and hasn't yet (its
        call failed or is still to come). "unknown" counts as an answer."""
        if not self.enabled:
            return False
        name = ai_name(key)
        if name is not None:
            return (name, direction) not in self.answers
        if not _askable(key):
            return False
        text = mask_description(description)
        return bool(text) and (text, direction) not in self.text_answers

    def _text_suggestion(
        self, key: RowKey, direction: str, categories: list[dict], description: str | None
    ) -> dict | None:
        if not self.text_answers or not _askable(key):
            return None
        text = mask_description(description)
        answer = self.text_answers.get((text, direction)) if text else None
        if not answer or not answer["name"] or _norm(answer["name"]) == "other":
            return None
        if answer["answer_type"] == "transfer":
            reason = next((r for r in _reasons_for(self.reasons, direction) if _norm(r["name"]) == _norm(answer["name"])),
                          None)
            return {"kind": "transfer", "transfer_reason_id": reason["id"] if reason else None, "category_id": None,
                    "category_name": reason["name"] if reason else answer["name"], "is_new_category": False,
                    "source": SOURCE, "merchant_name": None}
        own = [c for c in categories if c["type"] == _TYPE_FOR_DIRECTION[direction]]
        category = local_match(answer["name"], own)
        if category is not None and _norm(category["name"]) == "other":
            return None
        return _suggested(category, category["name"] if category else answer["name"])


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
        texts = wanted_texts(rows, keys)
        if not names and not texts:
            return AiContext()
        answers, user_map, text_answers, reasons = {}, {}, {}, []
        if names:
            answers = await repo.get_answers(conn, names=names)
            user_map = await repo.get_user_map(conn, user_id=user_id, categories_hash=categories_hash(categories))
        if texts:
            text_answers = await repo.get_text_answers(conn, user_id=user_id, texts=texts)
            reasons = await get_user_transfer_reasons(conn, user_id=user_id)
        missing = sum(n not in answers for n in names) + sum(t not in text_answers for t in texts)
        return AiContext(answers=answers, user_map=user_map, missing=missing, text_answers=text_answers,
                         reasons=reasons, enabled=inference_enabled())
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


async def begin_lookup(import_id: UUID) -> bool:
    """ab-338: flags a new upload's lookup as running before its rows are
    written and it is marked parsed, so a page polling it can't see it
    parsed without the lookup. The worker queues it once the rows are in
    (queue_lookup(begun=True)) or drops the flag (cancel_lookup). Never
    raises."""
    if not inference_enabled():
        return False
    try:
        from app.core.redis import get_redis

        redis = get_redis()
        await redis.delete(AI_DONE_KEY.format(import_id=import_id))
        await redis.set(AI_PENDING_KEY.format(import_id=import_id), "", ex=AI_PENDING_TTL_SECONDS)
        return True
    except Exception:
        logger.error("Flagging the AI category lookup for import %s failed", import_id, exc_info=True)
        return False


async def cancel_lookup(import_id: UUID) -> None:
    try:
        from app.core.redis import get_redis

        await get_redis().delete(AI_PENDING_KEY.format(import_id=import_id))
    except Exception:
        logger.warning("Dropping the AI lookup flag for import %s failed", import_id)


async def queue_lookup(import_id: UUID, *, fresh: bool = False, begun: bool = False) -> bool:
    """Queues the lookup for an import unless one is queued or running, or
    (unless fresh: a new upload) one finished in the last few hours. begun:
    begin_lookup already flagged it. Never raises."""
    if not inference_enabled():
        return False
    try:
        from app.core.redis import get_redis

        redis = get_redis()
        if not begun:
            done = AI_DONE_KEY.format(import_id=import_id)
            if fresh:
                await redis.delete(done)
            elif await redis.exists(done):
                return False
            if not await redis.set(AI_PENDING_KEY.format(import_id=import_id), "", nx=True, ex=AI_PENDING_TTL_SECONDS):
                return False
        await redis.lpush(AI_CATEGORY_JOBS_QUEUE, str(import_id))
        return True
    except Exception:
        logger.error("Queuing the AI category lookup for import %s failed", import_id, exc_info=True)
        return False


async def set_lookup_payees(import_id: UUID, count: int) -> None:
    """ab-338: how many payees the running lookup asks about, for the
    Processing step. Only while the flag is up - never revives it."""
    try:
        from app.core.redis import get_redis

        await get_redis().set(AI_PENDING_KEY.format(import_id=import_id), str(count), xx=True, keepttl=True)
    except Exception:
        logger.warning("Saving the AI lookup's payee count for import %s failed", import_id)


_NO_STATUS = {"state": None, "payees": None}


async def lookup_status(import_id: UUID) -> dict:
    """ab-338: {"state": "running" | "failed" | None, "payees": int | None}.
    failed = the last lookup (within a few hours) got no answers."""
    if not inference_enabled():
        return dict(_NO_STATUS)
    try:
        from app.core.redis import get_redis

        redis = get_redis()
        pending = await redis.get(AI_PENDING_KEY.format(import_id=import_id))
        if pending is not None:
            text = pending.decode() if isinstance(pending, bytes) else str(pending)
            return {"state": "running", "payees": int(text) if text.isdigit() else None}
        done = await redis.get(AI_DONE_KEY.format(import_id=import_id))
        failed = (done.decode() if isinstance(done, bytes) else done) == "failed"
        return {"state": "failed" if failed else None, "payees": None}
    except Exception:
        return dict(_NO_STATUS)


async def lookup_pending(import_id: UUID) -> bool:
    return (await lookup_status(import_id))["state"] == "running"


async def running_lookups(import_ids: list[UUID]) -> set[UUID]:
    """ab-338: which of a page of uploads have a lookup running."""
    if not import_ids or not inference_enabled():
        return set()
    try:
        from app.core.redis import get_redis

        flags = await get_redis().mget([AI_PENDING_KEY.format(import_id=i) for i in import_ids])
        return {i for i, flag in zip(import_ids, flags, strict=True) if flag is not None}
    except Exception:
        return set()


# ab-339: after a lookup that missed answers (the model busy or slow), the
# review may queue another this soon instead of hours later.
AI_RETRY_AFTER_SECONDS = 600


async def _clear_pending(import_id: UUID, *, failed: bool = False, retry_soon: bool = False) -> None:
    try:
        from app.core.redis import get_redis

        redis = get_redis()
        ttl = AI_RETRY_AFTER_SECONDS if retry_soon else AI_DONE_TTL_SECONDS
        await redis.set(AI_DONE_KEY.format(import_id=import_id), "failed" if failed else "1", ex=ttl)
        await redis.delete(AI_PENDING_KEY.format(import_id=import_id))
    except Exception:
        logger.warning("Clearing the AI lookup flag for import %s failed", import_id)


# ab-339: the model service sometimes answers 529 (overloaded), times out
# or replies empty; a batch is asked again after these pauses.
RETRY_WAITS_SECONDS = (5, 15)


async def _ask(prompt: str, *, retry_empty: bool = True) -> str | None:
    """The reply's text, or None when the call kept failing. ab-345: with
    retry_empty=False an empty reply comes back as "" at once, so a batch
    can be split instead of asked the same way three times."""
    settings = get_settings()
    for attempt, wait in enumerate((*RETRY_WAITS_SECONDS, None), start=1):
        try:
            reply = await complete(
                prompt, max_tokens=MAX_TOKENS, model=settings.do_category_model,
                timeout=settings.do_category_timeout_seconds,
            )
        except EmptyReply as e:
            if not retry_empty:
                logger.warning("AI category call returned nothing; splitting the batch")
                return ""
            logger.warning("AI category call failed (try %d): %s", attempt, e)
            if wait is None or not inference_enabled():
                return None
            await asyncio.sleep(wait)
            continue
        except InferenceError as e:
            # The lookup is then marked failed; say why in the worker log.
            logger.warning("AI category call failed (try %d): %s", attempt, e)
            if wait is None or not inference_enabled():
                return None
            await asyncio.sleep(wait)
            continue
        logger.info("AI category call: %s tokens in, %s out", reply.prompt_tokens, reply.completion_tokens)
        return reply.content
    return None


async def answer_names(conn, names: list[tuple[str, str]]) -> int:
    """Step 1 for names with no fresh answer: batches of 25, a few at a
    time. Returns how many answers were saved; a failed batch saves
    nothing, so its names are asked again next time. ab-345: an empty or
    unreadable reply splits the batch; names a reply skips are asked again."""
    model = get_settings().do_category_model

    async def save(answers: list[dict]) -> None:
        await repo.save_answers(conn, answers=answers, model=model)

    return await _answer_all(
        names, BATCH_SIZE, prompt=answer_prompt, parse=lambda text, batch: parse_answers(text, batch),
        key=lambda a: (a["name_key"], a["direction"]), save=save,
    )


async def answer_texts(
    conn, *, user_id: UUID, texts: list[tuple[str, str]], categories: list[dict], reasons: list[dict]
) -> int:
    """ab-339: this user's masked descriptions with no fresh answer, in
    batches of 15, saved for them only. Returns how many were saved."""
    model = get_settings().do_category_model

    async def save(answers: list[dict]) -> None:
        await repo.save_text_answers(conn, user_id=user_id, answers=answers, model=model)

    return await _answer_all(
        texts, TEXT_BATCH_SIZE, prompt=lambda batch: text_prompt(batch, categories, reasons),
        parse=lambda text, batch: parse_text_answers(text, batch, categories, reasons),
        key=lambda a: (a["text_key"], a["direction"]), save=save,
    )


async def _answer_all(items: list[tuple[str, str]], size: int, *, prompt, parse, key, save) -> int:
    """Asks about items in batches, a few calls at a time, saving each
    batch's answers. ab-345: a batch the model replies to with nothing (or
    with something unreadable) is split in half and each half asked, down
    to single items; items a reply skips are asked again on their own
    batch. A call that keeps failing (overloaded, timed out) is left for
    the next lookup - splitting wouldn't help."""
    gate = asyncio.Semaphore(CONCURRENT_CALLS)

    async def one(batch: list[tuple[str, str]]) -> int:
        async with gate:
            reply = await _ask(prompt(batch), retry_empty=len(batch) == 1)
        if reply is None:
            return 0
        answers = (parse(reply, batch) or []) if reply else []
        if answers:
            await save(answers)
        if len(batch) == 1:
            return len(answers)
        if not answers:
            half = len(batch) // 2
            parts = [batch[:half], batch[half:]]
        else:
            answered = {key(a) for a in answers}
            rest = [item for item in batch if item not in answered]
            parts = [rest] if rest else []
        return len(answers) + sum(await asyncio.gather(*(one(p) for p in parts)))

    batches = [items[i : i + size] for i in range(0, len(items), size)]
    return sum(await asyncio.gather(*(one(b) for b in batches)))


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
