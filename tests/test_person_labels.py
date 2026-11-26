"""ab-341: "Who is this?" - a private label for a person, saved with the
remembered choice and shown on their rows in later uploads."""

# ruff: noqa: F811 - test arguments named db are the imported fixture
from unittest.mock import AsyncMock

import pytest

import app.services.category_applies as svc
from app.services.allocations import AllocationRejected
from app.services.category_applies import ApplyGroup
from app.services.merchant_keys import PERSON as PERSON_NS
from tests.test_category_applies import GROCER, GROCERIES, PERSON, db, row, run  # noqa: F401


@pytest.fixture
def label_db(db, monkeypatch):
    db["set_person_label"] = AsyncMock()
    monkeypatch.setattr(svc, "set_person_label", db["set_person_label"])
    return db


def group(rows, **kw):
    return ApplyGroup(transaction_ids=[r["id"] for r in rows], category_id=GROCERIES["id"], **kw)


async def test_a_remembered_person_gets_the_label(label_db):
    a, b = row(PERSON), row(PERSON)
    await run(label_db, [a, b], [group([a, b], remember=True, label="  Mama mboga ")])

    label_db["set_person_label"].assert_awaited_once()
    kwargs = label_db["set_person_label"].await_args.kwargs
    assert kwargs["label"] == "Mama mboga"
    assert kwargs["provider"] == PERSON_NS and kwargs["key_value"].startswith("h:")
    # Saved after the choice it sits on.
    assert label_db["set_preferred_rules"].await_count == 1


async def test_an_empty_label_clears_it(label_db):
    a = row(PERSON)
    await run(label_db, [a], [group([a], remember=True, label="  ")])
    assert label_db["set_person_label"].await_args.kwargs["label"] is None


async def test_no_label_sent_leaves_it_alone(label_db):
    a = row(PERSON)
    await run(label_db, [a], [group([a], remember=True)])
    label_db["set_person_label"].assert_not_awaited()


async def test_a_label_needs_a_remembered_choice(label_db):
    a = row(PERSON)
    with pytest.raises(AllocationRejected) as exc:
        await run(label_db, [a], [group([a], label="Mechanic")])
    assert exc.value.status_code == 422 and "remember" in exc.value.detail.lower()


async def test_a_label_is_for_people_only(label_db):
    a = row(GROCER)
    with pytest.raises(AllocationRejected) as exc:
        await run(label_db, [a], [group([a], remember=True, label="My shop")])
    assert exc.value.status_code == 422
    label_db["set_person_label"].assert_not_awaited()


async def test_a_transfer_to_a_person_can_be_labelled(label_db):
    a = row(PERSON)
    await run(label_db, [a], [ApplyGroup(transaction_ids=[a["id"]], kind="transfer", remember=True, label="Brother")])
    assert label_db["set_person_label"].await_args.kwargs["label"] == "Brother"
