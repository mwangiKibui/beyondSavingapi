"""ab-339: AI suggestions from a row's description with its identifiers
masked. Names may be sent; numbers, references, codes and dates never are.
Answers are private to the user."""

import json
import re
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services import ai_categories, ai_category_job
from app.services.ai_categories import (
    AiContext,
    mask_description,
    parse_text_answers,
    text_prompt,
    wanted_texts,
    with_ai,
)
from app.services.merchant_keys import RowKey

GROCERIES = {"id": uuid4(), "name": "Groceries", "type": "expense"}
SUBS = {"id": uuid4(), "name": "Subscriptions", "type": "expense"}
OTHER_OUT = {"id": uuid4(), "name": "Other", "type": "expense"}
SALARY = {"id": uuid4(), "name": "Salary", "type": "income"}
CATEGORIES = [GROCERIES, SUBS, OTHER_OUT, SALARY]
TO_MY_ACCOUNT = {"id": uuid4(), "name": "Sent to another of my accounts", "direction": "out"}
FROM_MMF = {"id": uuid4(), "name": "From an investment / MMF", "direction": "in"}
ANY_WAY = {"id": uuid4(), "name": "Between my accounts", "direction": None}
REASONS = [TO_MY_ACCOUNT, FROM_MMF, ANY_WAY]


def other_key(**kw):
    return RowKey(kind="name", value="x", is_business=False, name=kw.pop("name", "X"), **kw)


# --- masking -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("description", "masked"),
    [
        ("TRANSFER TO M-PESA 254791569999", "TRANSFER TO M-PESA ***"),
        ("P752409114320447COOP CUSTODY CICCIC UNITTRUST", "*** CUSTODY CICCIC UNITTRUST"),
        ("01192832530700:Int.Coll:01-06-2024 to 27-06-2024 Interest", "***:INT.COLL:*** TO *** INTEREST"),
        ("EMEA>aws.amazon.c LU 259627 30130041008 99999 3759004364 *********", "EMEA>AWS.AMAZON.C LU ***"),
        ("MPS2ABCD~0712345~254700000000~VERONICA~ 2026-01-31", "*** VERONICA ***"),
        ("Pay to JANE WANJIKU Acc_40 ref QX12AB34", "PAY TO JANE WANJIKU ACC *** REF ***"),
    ],
)
def test_identifiers_are_masked_and_words_kept(description, masked):
    assert mask_description(description) == masked


def test_a_row_of_only_identifiers_is_not_sent():
    assert mask_description("0119283253 2024-01-02 QX12AB34") is None
    assert mask_description("TO 254700000000") is None  # no word worth asking about
    assert mask_description("") is None and mask_description(None) is None


def test_no_digit_ever_survives_masking():
    for text in ["A1 B22 C333 4D 5-6 7/8/9 x١٢٣", "ref:12ab-34cd/56", "abc123def ~ 9 ~ ok"]:
        masked = mask_description(text) or ""
        assert not re.search(r"\d", masked), masked


def test_long_text_is_capped():
    assert len(mask_description("WORD " * 200)) <= 300


# --- what is asked -----------------------------------------------------------


def test_wanted_texts_are_open_rows_that_are_not_business_names_or_handled():
    rows = [
        {"id": uuid4(), "direction": "out", "description": "TRANSFER TO M-PESA 254700000001"},
        {"id": uuid4(), "direction": "out", "description": "TRANSFER TO M-PESA 254700000002"},  # same text
        {"id": uuid4(), "direction": "out", "description": "GREENLEAF GROCERS"},  # business name: asked by name
        {"id": uuid4(), "direction": "out", "description": "EXCISE DUTY 123"},  # handled at import
        {"id": uuid4(), "direction": "out", "description": "PAID RENT 5", "allocated": 500.0},  # already allocated
        {"id": uuid4(), "direction": "in", "description": "OWN 1", "is_transfer": True},
        {"id": uuid4(), "direction": "in", "description": "254700000000"},  # nothing left
    ]
    keys = {
        rows[0]["id"]: other_key(),
        rows[1]["id"]: other_key(),
        rows[2]["id"]: RowKey(kind="name", value="g", is_business=True, name="GREENLEAF GROCERS"),
        rows[3]["id"]: other_key(is_charge=True),
        rows[4]["id"]: other_key(),
        rows[5]["id"]: other_key(),
        rows[6]["id"]: other_key(),
    }
    assert wanted_texts(rows, keys) == [("TRANSFER TO M-PESA ***", "out")]


# --- the prompt and its answers ----------------------------------------------


def test_the_prompt_lists_the_users_categories_and_reasons_never_other():
    prompt = text_prompt([("TRANSFER TO M-PESA ***", "out")], CATEGORIES, REASONS)
    assert '"TRANSFER TO M-PESA ***" (money out)' in prompt
    assert '"Groceries"' in prompt and '"Salary"' in prompt
    assert '"Sent to another of my accounts"' in prompt and '"From an investment / MMF"' in prompt
    assert '"Between my accounts"' in prompt
    money_out = next(line for line in prompt.splitlines() if line.startswith("Money out: ["))
    assert '"Groceries"' in money_out and '"Other"' not in money_out
    assert not re.search(r"\d{3}", prompt)


BATCH = [
    ("TRANSFER TO M-PESA ***", "out"),
    ("*** CUSTODY CICCIC UNITTRUST", "in"),
    ("EMEA>AWS.AMAZON.C LU ***", "out"),
    ("***:INT.COLL:*** TO *** INTEREST", "out"),
    ("*** VERONICA ***", "in"),
    ("LOAN DISBURSEMENT ***", "in"),
    ("SHOP ***", "out"),
    ("WEIRD ***", "out"),
]


def reply(*answers):
    return json.dumps({"answers": [dict(n=i, **a) for i, a in enumerate(answers, start=1)]})


def test_answers_become_categories_transfers_or_unknown():
    text = reply(
        {"type": "transfer", "name": "sent to another of my accounts"},
        {"type": "transfer", "name": "From an investment / MMF"},
        {"type": "category", "name": "Subscription", "new": False},
        {"type": "category", "name": "Loan interest", "new": True},
        {"type": "unknown"},
        {"type": "category", "name": "Loan Received", "new": True},  # borrowed money is never income
        {"type": "category", "name": "Other"},  # tells the user nothing
        {"type": "transfer", "name": "Made up reason"},  # not one of theirs
    )
    got = {(a["text_key"], a["direction"]): a for a in parse_text_answers(text, BATCH, CATEGORIES, REASONS)}
    pick = lambda i: (lambda a: (a["answer_type"], a["name"], a["is_new"]))(got[BATCH[i]])  # noqa: E731
    assert pick(0) == ("transfer", "Sent to another of my accounts", False)
    assert pick(1) == ("transfer", "From an investment / MMF", False)
    assert pick(2) == ("category", "Subscriptions", False)  # their own category's spelling
    assert pick(3) == ("category", "Loan interest", True)
    assert pick(4) == ("unknown", None, False)
    assert pick(5) == ("unknown", None, False)
    assert pick(6) == ("unknown", None, False)
    assert pick(7) == ("unknown", None, False)


def test_a_transfer_reason_for_the_other_direction_is_refused():
    text = reply({"type": "transfer", "name": "From an investment / MMF"})
    [answer] = parse_text_answers(text, BATCH[:1], CATEGORIES, REASONS)  # money out
    assert answer["answer_type"] == "unknown"


def test_a_reply_that_is_not_json_saves_nothing():
    assert parse_text_answers("sorry", BATCH, CATEGORIES, REASONS) is None


# --- the review ---------------------------------------------------------------


def ctx(answer_type, name, is_new=False, text="TRANSFER TO M-PESA ***", direction="out"):
    answers = {(text, direction): {"answer_type": answer_type, "name": name, "is_new": is_new}}
    return AiContext(text_answers=answers, reasons=REASONS)


def test_a_transfer_answer_is_a_funds_transfer_suggestion():
    s = ctx("transfer", "Sent to another of my accounts").suggestion(
        other_key(), "out", CATEGORIES, "TRANSFER TO M-PESA 254700000000")
    assert s["kind"] == "transfer" and s["transfer_reason_id"] == TO_MY_ACCOUNT["id"]
    assert (s["category_name"], s["source"]) == ("Sent to another of my accounts", "ai")


def test_a_category_answer_uses_the_users_category_or_a_new_one():
    s = ctx("category", "Subscriptions").suggestion(other_key(), "out", CATEGORIES, "TRANSFER TO M-PESA 1234")
    assert (s["category_id"], s["is_new_category"]) == (SUBS["id"], False)
    s = ctx("category", "Loan interest", is_new=True).suggestion(other_key(), "out", CATEGORIES, "TRANSFER TO M-PESA 1")
    assert (s["category_id"], s["category_name"], s["is_new_category"]) == (None, "Loan interest", True)


def test_unknown_or_other_texts_get_no_suggestion():
    assert ctx("unknown", None).suggestion(other_key(), "out", CATEGORIES, "TRANSFER TO M-PESA 1") is None
    assert ctx("category", "Other").suggestion(other_key(), "out", CATEGORIES, "TRANSFER TO M-PESA 1") is None


def test_a_text_answer_replaces_a_keyword_guess_but_never_the_users_choice():
    ai = ctx("transfer", "Sent to another of my accounts").suggestion(
        other_key(), "out", CATEGORIES, "TRANSFER TO M-PESA 1")
    keyword = {"category_name": "Transport", "source": "keyword"}
    mine = {"category_name": "Transport", "source": "your_rule"}
    assert with_ai(keyword, ai) is ai
    assert with_ai(mine, ai) is mine


async def test_the_review_loads_this_users_text_answers(monkeypatch):
    uid = uuid4()
    rows = [{"id": uuid4(), "direction": "out", "description": "TRANSFER TO M-PESA 254700000001"},
            {"id": uuid4(), "direction": "in", "description": "*** CUSTODY CICCIC UNITTRUST 1"}]
    keys = {r["id"]: other_key() for r in rows}
    known = {("TRANSFER TO M-PESA ***", "out"): {"answer_type": "unknown", "name": None, "is_new": False}}
    get = AsyncMock(return_value=known)
    monkeypatch.setattr(ai_categories.repo, "get_text_answers", get)
    monkeypatch.setattr(ai_categories, "get_user_transfer_reasons", AsyncMock(return_value=REASONS))
    context = await ai_categories.load_ai_context(object(), user_id=uid, rows=rows, keys=keys, categories=CATEGORIES)
    assert get.await_args.kwargs["user_id"] == uid
    assert context.missing == 1 and context.reasons == REASONS


# --- the job -----------------------------------------------------------------


async def test_the_job_asks_this_users_missing_texts_and_saves_them_for_them_only(monkeypatch):
    iid, uid = uuid4(), uuid4()
    record = {"id": iid, "account_id": uuid4(), "period_start": None, "period_end": None,
              "provider": "Co-operative Bank", "user_id": uid}
    rows = [{"id": uuid4(), "direction": "out", "description": "TRANSFER TO M-PESA 254700000001"}]
    monkeypatch.setattr(ai_category_job.repo, "get_import_for_lookup", AsyncMock(return_value=record))
    monkeypatch.setattr(ai_category_job, "get_import_rows", AsyncMock(return_value=rows))
    monkeypatch.setattr(ai_category_job, "get_directory", AsyncMock())
    monkeypatch.setattr(ai_category_job, "extract_keys", lambda rows, **k: {r["id"]: other_key() for r in rows})
    monkeypatch.setattr(ai_category_job, "learned_name_lookups", lambda *a: [])
    monkeypatch.setattr(ai_category_job, "get_learned_names", AsyncMock(return_value={}))
    monkeypatch.setattr(ai_category_job, "apply_learned_names", lambda rows, keys, *a: keys)
    monkeypatch.setattr(ai_category_job, "get_user_categories", AsyncMock(return_value=CATEGORIES))
    monkeypatch.setattr(ai_category_job, "get_user_transfer_reasons", AsyncMock(return_value=REASONS))
    monkeypatch.setattr(ai_category_job.repo, "get_text_answers", AsyncMock(return_value={}))
    asked = []

    async def ask(prompt):
        asked.append(prompt)
        return reply({"type": "transfer", "name": "Sent to another of my accounts"})

    monkeypatch.setattr(ai_categories, "_ask", ask)
    save = AsyncMock()
    monkeypatch.setattr(ai_categories.repo, "save_text_answers", save)
    payees = AsyncMock()
    monkeypatch.setattr(ai_category_job.ai_categories, "set_lookup_payees", payees)
    monkeypatch.setattr(ai_category_job.ai_categories, "_clear_pending", AsyncMock())

    await ai_category_job.run_lookup(object(), iid)

    assert len(asked) == 1 and "254700000001" not in asked[0] and "TRANSFER TO M-PESA ***" in asked[0]
    payees.assert_awaited_once_with(iid, 1)
    kwargs = save.await_args.kwargs
    assert kwargs["user_id"] == uid
    assert kwargs["answers"] == [{"text_key": "TRANSFER TO M-PESA ***", "direction": "out",
                                  "answer_type": "transfer", "name": "Sent to another of my accounts",
                                  "is_new": False}]
    ai_category_job.ai_categories._clear_pending.assert_awaited_once_with(iid, failed=False, retry_soon=False)


async def test_a_lookup_that_missed_some_answers_may_run_again_soon(monkeypatch):
    """Two texts asked, one batch answered: the review can queue the rest
    in minutes, not hours."""
    monkeypatch.setattr(ai_category_job, "_run", AsyncMock(return_value="partial"))
    clear = AsyncMock()
    monkeypatch.setattr(ai_category_job.ai_categories, "_clear_pending", clear)
    iid = uuid4()
    await ai_category_job.run_lookup(object(), iid)
    clear.assert_awaited_once_with(iid, failed=False, retry_soon=True)


async def test_the_done_flag_lasts_minutes_after_a_partial_lookup(monkeypatch):
    seen = {}

    class R:
        async def set(self, key, value, ex=None, **kw):
            seen[key] = ex

        async def delete(self, key):
            pass

    monkeypatch.setattr("app.core.redis.get_redis", lambda: R())
    iid = uuid4()
    await ai_categories._clear_pending(iid, retry_soon=True)
    assert seen[f"ai_category_done:{iid}"] == ai_categories.AI_RETRY_AFTER_SECONDS
    await ai_categories._clear_pending(iid)
    assert seen[f"ai_category_done:{iid}"] == 6 * 3600


async def test_descriptions_are_asked_15_at_a_time(monkeypatch):
    texts = [(f"SHOP {chr(65 + i // 26)}{chr(65 + i % 26)} ***", "out") for i in range(31)]
    sizes = []

    async def ask(prompt):
        sizes.append(prompt.count("(money out)"))
        return None

    monkeypatch.setattr(ai_categories, "_ask", ask)
    saved = await ai_categories.answer_texts(object(), user_id=uuid4(), texts=texts, categories=CATEGORIES,
                                             reasons=REASONS)
    assert saved == 0 and sorted(sizes) == [1, 15, 15]
