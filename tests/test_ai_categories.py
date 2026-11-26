"""ab-336: AI category suggestions. The model, database and Redis are
faked, so nothing is sent anywhere. Names are made up."""

import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services import ai_categories, ai_category_job
from app.services.ai_categories import (
    AiContext,
    ai_name,
    answer_prompt,
    categories_hash,
    local_match,
    map_prompt,
    parse_answers,
    parse_map,
    wanted_names,
    with_ai,
)
from app.services.inference import InferenceError, InferenceReply
from app.services.merchant_keys import RowKey, extract_key

SECRET = "test-secret"


def business(name="GREENLEAF GROCERS", **kw) -> RowKey:
    return RowKey(kind="name", value=name, is_business=True, name=name, **kw)


def cat(name, type_="expense"):
    return {"id": uuid4(), "name": name, "type": type_, "entry_type": None}


# --- what may be sent ----------------------------------------------------------


def test_only_business_names_are_sent():
    assert ai_name(business()) == "GREENLEAF GROCERS"
    assert ai_name(business("  sunrise   car wash ")) == "SUNRISE CAR WASH"
    assert ai_name(RowKey(kind="reference", value="h:1", is_person=True, name="JANE DOE")) is None
    assert ai_name(RowKey(kind="name", value="JOSEPH", is_business=False, name="JOSEPH")) is None
    assert ai_name(RowKey(kind="paybill", value="888880", is_business=True, number="888880")) is None  # no name
    assert ai_name(business(is_charge=True)) is None
    assert ai_name(business(row_kind="loan_payout")) is None
    assert ai_name(business("123 456")) is None


def test_business_words_now_cover_shops_like_grocers_and_car_washes():
    for text in ("GREENLEAF GROCERS", "SUNRISE CAR WASH", "TOPLINE PRINTERS"):
        key = extract_key(provider="Co-operative Bank", description=text, direction="out", secret=SECRET)
        assert ai_name(key) == text
    # A single word that could be a person's name is never sent.
    key = extract_key(provider="Co-operative Bank", description="VERONICA", direction="out", secret=SECRET)
    assert ai_name(key) is None


def test_wanted_names_are_unique_per_direction():
    a, b, c = uuid4(), uuid4(), uuid4()
    rows = [{"id": a, "direction": "out"}, {"id": b, "direction": "out"}, {"id": c, "direction": "in"}]
    keys = {a: business(), b: business(), c: business()}
    assert wanted_names(rows, keys) == [("GREENLEAF GROCERS", "out"), ("GREENLEAF GROCERS", "in")]


def test_the_prompt_holds_names_and_category_names_only():
    prompt = answer_prompt([("GREENLEAF GROCERS", "out")])
    assert '"GREENLEAF GROCERS" (money out)' in prompt
    assert "Groceries" in prompt and "Salary" in prompt


# --- step 1 answers ------------------------------------------------------------


def test_answers_are_read_per_payee():
    batch = [("GREENLEAF GROCERS", "out"), ("APEX SOLUTIONS LTD", "out"), ("AVECHI", "out"), ("SKIPPED", "out")]
    reply = "Sure!\n" + json.dumps({"answers": [
        {"n": 1, "category": "Groceries", "new": False, "confidence": "high"},
        {"n": 2, "category": "unknown", "new": False, "confidence": "low"},
        {"n": 3, "category": "Electronics", "new": True, "confidence": "medium"},
    ]})
    out = parse_answers(reply, batch)
    assert [(a["name_key"], a["category_name"], a["is_new"]) for a in out] == [
        ("GREENLEAF GROCERS", "Groceries", False),
        ("APEX SOLUTIONS LTD", None, False),
        ("AVECHI", "Electronics", True),
    ]  # the skipped payee gets no answer, so it is asked again


@pytest.mark.parametrize("text", ["no json here", "{not json}", json.dumps({"answer": []})])
def test_a_reply_that_isnt_the_json_asked_for_saves_nothing(text):
    assert parse_answers(text, [("X SHOP", "out")]) is None


def test_a_loan_paid_in_is_never_a_category():
    out = parse_answers(json.dumps({"answers": [{"n": 1, "category": "Loan Received", "new": True}]}),
                        [("BUSINESS LOANS LTD", "in")])
    assert out[0]["category_name"] is None


def test_other_is_no_suggestion():
    assert _ctx("Other").suggestion(business(), "out", [cat("Other")]) is None
    other = cat("Other")
    mapped = _ctx("Mobile money", user_map={("Mobile money", "expense"): other["id"]})
    assert mapped.suggestion(business(), "out", [other]) is None


def test_bus_alone_doesnt_mark_a_business():
    key = extract_key(provider="Co-operative Bank", description="BUS REFINANCE", direction="in", secret=SECRET)
    assert ai_name(key) is None


def test_a_category_with_digits_counts_as_unknown():
    out = parse_answers(json.dumps({"answers": [{"n": 1, "category": "Till 12345"}]}), [("X SHOP", "out")])
    assert out[0]["category_name"] is None


# --- step 2: onto the user's categories ------------------------------------------


def test_local_match_by_name_plural_and_ampersand():
    grocery = cat("Grocery")
    assert local_match("Groceries", [grocery, cat("Fuel")]) is grocery
    charges = cat("Bank and Mobile Charges")
    assert local_match("Bank & mobile charges", [charges]) is charges


def test_local_match_by_synonym_only_when_one_category_fits():
    fuel = cat("Fuel")
    assert local_match("Transport", [fuel, cat("Rent")]) is fuel
    assert local_match("Transport", [fuel, cat("Petrol")]) is None  # two could: ask the model
    assert local_match("Insurance", [fuel]) is None


def test_categories_hash_changes_with_the_list():
    a = [cat("Grocery"), cat("Salary", "income")]
    assert categories_hash(a) == categories_hash(list(reversed(a)))
    assert categories_hash(a) != categories_hash([*a, cat("Fuel")])


def test_parse_map_keeps_only_the_users_own_categories():
    food = cat("Food")
    cats = [food, cat("Pay", "income")]
    items = [("Groceries", "expense"), ("Electronics", "expense"), ("Clothing", "expense")]
    reply = json.dumps({"answers": [{"n": 1, "category": "Food"}, {"n": 2, "category": "none"},
                                    {"n": 3, "category": "Made Up"}]})
    assert parse_map(reply, items, cats) == {("Groceries", "expense"): food["id"], ("Electronics", "expense"): None}
    assert '"Groceries" (money out)' in map_prompt(items, cats) and "GREENLEAF" not in map_prompt(items, cats)


# --- the review reads the caches -------------------------------------------------


def _ctx(category_name, **kw):
    answers = {("GREENLEAF GROCERS", "out"): {"category_name": category_name, "is_new": False}}
    return AiContext(answers=answers, **kw)


def test_an_answer_becomes_the_users_own_category():
    grocery = cat("Grocery")
    s = _ctx("Groceries").suggestion(business(), "out", [grocery])
    assert (s["category_id"], s["category_name"], s["is_new_category"], s["source"]) == (
        grocery["id"], "Grocery", False, "ai")


def test_a_mapped_answer_uses_the_users_mapping():
    food = cat("Food")
    s = _ctx("Groceries", user_map={("Groceries", "expense"): food["id"]}).suggestion(business(), "out", [food])
    assert s["category_id"] == food["id"]


def test_an_answer_none_of_theirs_fits_is_suggested_as_new():
    s = _ctx("Electronics", user_map={("Electronics", "expense"): None}).suggestion(business(), "out", [cat("Rent")])
    assert (s["category_id"], s["category_name"], s["is_new_category"]) == (None, "Electronics", True)


def test_no_suggestion_until_mapped_unknown_or_not_a_business():
    assert _ctx("Groceries").suggestion(business(), "out", [cat("Food"), cat("Snacks")]) is None
    assert _ctx(None).suggestion(business(), "out", [cat("Groceries")]) is None
    person = RowKey(kind="reference", value="h:1", is_person=True, name="GREENLEAF GROCERS")
    assert _ctx("Groceries").suggestion(person, "out", [cat("Groceries")]) is None


AI = {"category_id": uuid4(), "category_name": "Printing", "is_new_category": False, "source": "ai"}


def test_the_ai_fills_a_gap_and_replaces_a_keyword_guess_it_disagrees_with():
    assert with_ai(None, AI) is AI
    transfer = {"kind": "transfer", "category_name": "Sent to someone", "source": "keyword"}
    assert with_ai(transfer, AI) is AI
    other = {"category_name": "Household", "source": "keyword"}
    assert with_ai(other, AI) is AI
    same = {"category_name": "printing", "source": "keyword"}
    assert with_ai(same, AI) is same


@pytest.mark.parametrize("source", ["your_rule", "this_statement", "others", "directory", "own_account"])
def test_the_users_choices_and_shared_answers_are_never_replaced(source):
    mine = {"category_name": "Household", "source": source}
    assert with_ai(mine, AI) is mine


def test_build_preview_rows_uses_the_ai_last():
    from app.services.category_suggestions import Directory, build_preview_rows

    grocery = cat("Grocery")
    shop, person = uuid4(), uuid4()
    rows = [
        {"id": shop, "direction": "out", "description": "GREENLEAF GROCERS", "amount": 10.0, "txn_date": None,
         "currency": "KES", "allocated": 0, "category_id": None, "is_transfer": False},
        {"id": person, "direction": "out", "description": "JANE", "amount": 5.0, "txn_date": None,
         "currency": "KES", "allocated": 0, "category_id": None, "is_transfer": False},
    ]
    keys = {shop: business(), person: RowKey(kind="reference", value="h:2", is_person=True, name="JANE")}
    out = build_preview_rows(rows, keys=keys, rules=[], directory=Directory.from_rows([], blocked=[]), categories=[grocery],
                             ai=_ctx("Groceries"))
    by_id = {r["transaction_id"]: r["suggestion"] for r in out}
    assert by_id[shop]["source"] == "ai" and by_id[shop]["category_id"] == grocery["id"]
    assert by_id[person] is None


# --- the lookup job ---------------------------------------------------------------


def reply(answer: dict) -> InferenceReply:
    return InferenceReply(content=json.dumps(answer), model="glm-5.3-flash", prompt_tokens=10, completion_tokens=20)


async def test_answer_names_asks_in_batches_and_saves(monkeypatch):
    names = [(f"SHOP {chr(65 + i)} STORES", "out") for i in range(30)]
    prompts = []

    async def fake(prompt, **kw):
        prompts.append((prompt, kw["model"]))
        n = prompt.count("(money out)")
        return reply({"answers": [{"n": i, "category": "Groceries"} for i in range(1, n + 1)]})

    monkeypatch.setattr(ai_categories, "complete", fake)
    save = AsyncMock()
    monkeypatch.setattr(ai_categories.repo, "save_answers", save)
    assert await ai_categories.answer_names(object(), names) == 30
    assert sorted(p.count("(money out)") for p, _ in prompts) == [5, 25]
    assert {m for _, m in prompts} == {"glm-5.3-flash"}
    assert save.await_count == 2


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch):
    monkeypatch.setattr(ai_categories, "RETRY_WAITS_SECONDS", (0, 0))


async def test_ab339_a_busy_or_empty_call_is_tried_again(monkeypatch):
    """The model service answers 529 (overloaded), times out or replies
    empty now and then: a batch is asked up to three times."""
    monkeypatch.setattr(ai_categories, "inference_enabled", lambda: True)
    ok = InferenceReply(content="fine", model="m", prompt_tokens=1, completion_tokens=1)
    complete = AsyncMock(side_effect=[InferenceError("HTTP 529"), InferenceError("Empty inference reply"), ok])
    monkeypatch.setattr(ai_categories, "complete", complete)
    assert await ai_categories._ask("prompt") == "fine"
    assert complete.await_count == 3


async def test_ab339_a_call_that_keeps_failing_gives_up_after_three_tries(monkeypatch):
    monkeypatch.setattr(ai_categories, "inference_enabled", lambda: True)
    complete = AsyncMock(side_effect=InferenceError("HTTP 529"))
    monkeypatch.setattr(ai_categories, "complete", complete)
    assert await ai_categories._ask("prompt") is None
    assert complete.await_count == 3


async def test_ab339_no_key_is_not_retried(monkeypatch):
    complete = AsyncMock(side_effect=InferenceError("No inference key configured"))
    monkeypatch.setattr(ai_categories, "complete", complete)
    assert await ai_categories._ask("prompt") is None
    assert complete.await_count == 1


async def test_a_failed_call_saves_nothing_and_never_raises(monkeypatch):
    monkeypatch.setattr(ai_categories, "complete", AsyncMock(side_effect=InferenceError("down")))
    save = AsyncMock()
    monkeypatch.setattr(ai_categories.repo, "save_answers", save)
    assert await ai_categories.answer_names(object(), [("SHOP STORES", "out")]) == 0
    save.assert_not_awaited()


async def test_map_for_user_asks_only_for_what_the_local_match_cant_place(monkeypatch):
    food, snacks = cat("Food"), cat("Snacks")
    monkeypatch.setattr(ai_categories.repo, "get_user_map", AsyncMock(return_value={}))
    save = AsyncMock()
    monkeypatch.setattr(ai_categories.repo, "save_user_map", save)
    asked = []

    async def fake(prompt, **kw):
        asked.append(prompt)
        return reply({"answers": [{"n": 1, "category": "Food"}]})

    monkeypatch.setattr(ai_categories, "complete", fake)
    answers = [
        {"direction": "out", "category_name": "Groceries"},  # needs the model
        {"direction": "out", "category_name": "Snacks"},  # same name: local
        {"direction": "out", "category_name": None},  # unknown: nothing to map
    ]
    uid = uuid4()
    assert await ai_categories.map_for_user(object(), user_id=uid, categories=[food, snacks], answers=answers) == 1
    assert len(asked) == 1 and '"Groceries"' in asked[0] and '"Snacks" (money' not in asked[0]
    assert save.await_args.kwargs["mapping"] == {("Groceries", "expense"): food["id"]}


class FakeRedis:
    def __init__(self):
        self.store, self.queue = {}, []

    async def set(self, key, value, nx=False, ex=None, xx=False, keepttl=False):
        if nx and key in self.store:
            return None
        if xx and key not in self.store:
            return None
        self.store[key] = value
        return True

    async def get(self, key):
        return self.store.get(key)

    async def mget(self, keys):
        return [self.store.get(k) for k in keys]

    async def exists(self, key):
        return int(key in self.store)

    async def delete(self, key):
        self.store.pop(key, None)

    async def lpush(self, queue, value):
        self.queue.append((queue, value))


@pytest.fixture
def fake_redis(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr("app.core.redis.get_redis", lambda: r)
    monkeypatch.setattr(ai_categories, "inference_enabled", lambda: True)
    return r


async def test_a_lookup_is_queued_once_until_it_finishes(fake_redis):
    iid = uuid4()
    assert await ai_categories.queue_lookup(iid) is True
    assert await ai_categories.queue_lookup(iid) is False
    assert await ai_categories.lookup_pending(iid) is True
    assert fake_redis.queue == [("ai_category_jobs", str(iid))]
    await ai_categories._clear_pending(iid)
    assert await ai_categories.lookup_pending(iid) is False
    # Reopening the review doesn't queue it again for a while; a new upload does.
    assert await ai_categories.queue_lookup(iid) is False
    assert await ai_categories.queue_lookup(iid, fresh=True) is True


async def test_ab338_a_begun_lookup_shows_as_running_before_it_is_queued(fake_redis):
    iid = uuid4()
    fake_redis.store[f"ai_category_done:{iid}"] = "1"
    assert await ai_categories.begin_lookup(iid) is True
    assert await ai_categories.lookup_status(iid) == {"state": "running", "payees": None}
    assert fake_redis.queue == []
    # The worker queues it once the rows are written, without a second flag.
    assert await ai_categories.queue_lookup(iid, begun=True) is True
    assert fake_redis.queue == [("ai_category_jobs", str(iid))]


async def test_ab338_a_cancelled_lookup_is_not_running(fake_redis):
    iid = uuid4()
    await ai_categories.begin_lookup(iid)
    await ai_categories.cancel_lookup(iid)
    assert await ai_categories.lookup_status(iid) == {"state": None, "payees": None}


async def test_ab338_the_status_carries_the_payee_count_then_the_outcome(fake_redis):
    iid = uuid4()
    await ai_categories.begin_lookup(iid)
    await ai_categories.set_lookup_payees(iid, 25)
    assert await ai_categories.lookup_status(iid) == {"state": "running", "payees": 25}
    await ai_categories._clear_pending(iid, failed=True)
    assert await ai_categories.lookup_status(iid) == {"state": "failed", "payees": None}
    await ai_categories.begin_lookup(iid)
    await ai_categories._clear_pending(iid)
    assert await ai_categories.lookup_status(iid) == {"state": None, "payees": None}


async def test_ab338_the_payee_count_never_revives_a_finished_lookup(fake_redis):
    iid = uuid4()
    await ai_categories.set_lookup_payees(iid, 3)
    assert await ai_categories.lookup_status(iid) == {"state": None, "payees": None}


async def test_ab338_running_lookups_for_a_page_of_uploads(fake_redis):
    a, b = uuid4(), uuid4()
    await ai_categories.begin_lookup(a)
    assert await ai_categories.running_lookups([a, b]) == {a}
    assert await ai_categories.running_lookups([]) == set()


async def test_ab338_status_is_quiet_without_redis_or_a_key(monkeypatch):
    monkeypatch.setattr(ai_categories, "inference_enabled", lambda: False)
    assert await ai_categories.begin_lookup(uuid4()) is False
    assert await ai_categories.lookup_status(uuid4()) == {"state": None, "payees": None}
    assert await ai_categories.running_lookups([uuid4()]) == set()


async def test_nothing_is_queued_without_a_key(monkeypatch):
    monkeypatch.setattr(ai_categories, "inference_enabled", lambda: False)
    assert await ai_categories.queue_lookup(uuid4()) is False
    assert await ai_categories.lookup_pending(uuid4()) is False


async def test_the_job_asks_only_for_names_without_an_answer(monkeypatch, fake_redis):
    iid, uid, a, b = uuid4(), uuid4(), uuid4(), uuid4()
    record = {"id": iid, "account_id": uuid4(), "period_start": None, "period_end": None,
              "provider": "Co-operative Bank", "user_id": uid}
    rows = [{"id": a, "direction": "out", "description": "GREENLEAF GROCERS"},
            {"id": b, "direction": "out", "description": "SUNRISE CAR WASH"}]
    monkeypatch.setattr(ai_category_job.repo, "get_import_for_lookup", AsyncMock(return_value=record))
    monkeypatch.setattr(ai_category_job, "get_import_rows", AsyncMock(return_value=rows))
    monkeypatch.setattr(ai_category_job, "get_directory", AsyncMock())
    monkeypatch.setattr(ai_category_job, "learned_name_lookups", lambda *a: [])
    monkeypatch.setattr(ai_category_job, "get_learned_names", AsyncMock(return_value={}))
    monkeypatch.setattr(ai_category_job, "apply_learned_names", lambda rows, keys, *a: keys)
    known = {("GREENLEAF GROCERS", "out"): {"category_name": "Groceries", "direction": "out"}}
    monkeypatch.setattr(ai_category_job.repo, "get_answers", AsyncMock(return_value=known))
    answer = AsyncMock(return_value=1)
    monkeypatch.setattr(ai_category_job.ai_categories, "answer_names", answer)
    monkeypatch.setattr(ai_category_job, "get_user_categories", AsyncMock(return_value=[]))
    mapped = AsyncMock(return_value=0)
    monkeypatch.setattr(ai_category_job.ai_categories, "map_for_user", mapped)
    fake_redis.store[f"ai_category_pending:{iid}"] = ""
    payees = AsyncMock()
    monkeypatch.setattr(ai_category_job.ai_categories, "set_lookup_payees", payees)

    await ai_category_job.run_lookup(object(), iid)

    payees.assert_awaited_once_with(iid, 1)  # ab-338: the one name it asks about
    assert answer.await_args.args[1] == [("SUNRISE CAR WASH", "out")]
    assert mapped.await_args.kwargs["user_id"] == uid
    assert await ai_categories.lookup_pending(iid) is False
    assert await ai_categories.lookup_status(iid) == {"state": None, "payees": None}


async def test_ab338_a_job_whose_answers_all_fail_is_marked_failed(monkeypatch, fake_redis):
    iid = uuid4()
    record = {"id": iid, "account_id": uuid4(), "period_start": None, "period_end": None,
              "provider": "Co-operative Bank", "user_id": uuid4()}
    rows = [{"id": uuid4(), "direction": "out", "description": "SUNRISE CAR WASH"}]
    monkeypatch.setattr(ai_category_job.repo, "get_import_for_lookup", AsyncMock(return_value=record))
    monkeypatch.setattr(ai_category_job, "get_import_rows", AsyncMock(return_value=rows))
    monkeypatch.setattr(ai_category_job, "get_directory", AsyncMock())
    monkeypatch.setattr(ai_category_job, "learned_name_lookups", lambda *a: [])
    monkeypatch.setattr(ai_category_job, "get_learned_names", AsyncMock(return_value={}))
    monkeypatch.setattr(ai_category_job, "apply_learned_names", lambda rows, keys, *a: keys)
    monkeypatch.setattr(ai_category_job.repo, "get_answers", AsyncMock(return_value={}))
    monkeypatch.setattr(ai_category_job.ai_categories, "answer_names", AsyncMock(return_value=0))
    monkeypatch.setattr(ai_category_job, "get_user_categories", AsyncMock(return_value=[]))
    monkeypatch.setattr(ai_category_job.ai_categories, "map_for_user", AsyncMock(return_value=0))
    await ai_categories.begin_lookup(iid)

    await ai_category_job.run_lookup(object(), iid)

    assert await ai_categories.lookup_status(iid) == {"state": "failed", "payees": None}


async def test_the_job_never_raises(monkeypatch, fake_redis):
    monkeypatch.setattr(ai_category_job.repo, "get_import_for_lookup", AsyncMock(side_effect=RuntimeError("db")))
    iid = uuid4()
    await ai_category_job.run_lookup(object(), iid)
    assert (await ai_categories.lookup_status(iid))["state"] == "failed"
