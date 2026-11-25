"""ab-309: check_limit / enforce_limit. Repositories are mocked; the SQL is
checked against the dev database in the live run."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import app.services.limits as svc
from app.services.limits import LIMIT_KEYS, LimitReached, check_limit, enforce_limit, nairobi_day

USER = uuid4()
UTC = timezone.utc
EAT = timezone(timedelta(hours=3))


@pytest.fixture
def repo(monkeypatch):
    mocks = {
        "get_plan_limit": AsyncMock(return_value=None),
        "count_imports_reached_allocation": AsyncMock(return_value=0),
        "count_active_accounts": AsyncMock(return_value=0),
        "count_proposals": AsyncMock(return_value=0),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(svc, name, mock)
    return mocks


def test_the_known_keys_live_in_one_place():
    assert set(LIMIT_KEYS) == {"statement_imports_per_day", "accounts_max", "ai_layout_proposals_per_day"}


# --- Nairobi day -------------------------------------------------------------


def test_2230_utc_belongs_to_the_next_eat_day():
    start, end = nairobi_day(datetime(2026, 11, 24, 22, 30, tzinfo=UTC))
    assert start == datetime(2026, 11, 25, 0, 0, tzinfo=EAT)
    assert end == datetime(2026, 11, 26, 0, 0, tzinfo=EAT)


def test_2059_utc_is_2359_eat_still_the_same_day():
    start, end = nairobi_day(datetime(2026, 11, 24, 20, 59, tzinfo=UTC))
    assert start == datetime(2026, 11, 24, 0, 0, tzinfo=EAT)
    assert end == datetime(2026, 11, 25, 0, 0, tzinfo=EAT)


def test_midnight_eat_starts_a_new_day():
    start, _ = nairobi_day(datetime(2026, 11, 25, 0, 0, tzinfo=EAT))
    assert start == datetime(2026, 11, 25, 0, 0, tzinfo=EAT)


async def test_imports_are_counted_over_the_eat_day_and_reset_at_eat_midnight(repo):
    repo["get_plan_limit"].return_value = 5
    repo["count_imports_reached_allocation"].return_value = 2

    status = await check_limit(object(), USER, "statement_imports_per_day",
                               now=datetime(2026, 11, 24, 22, 30, tzinfo=UTC))

    kwargs = repo["count_imports_reached_allocation"].await_args.kwargs
    assert kwargs["user_id"] == USER
    assert kwargs["start"] == datetime(2026, 11, 25, 0, 0, tzinfo=EAT)
    assert kwargs["end"] == datetime(2026, 11, 26, 0, 0, tzinfo=EAT)
    assert (status.used, status.limit) == (2, 5)
    assert status.resets_at.isoformat() == "2026-11-26T00:00:00+03:00"


# --- check_limit ------------------------------------------------------------


async def test_unlimited_skips_counting(repo):
    status = await check_limit(object(), USER, "statement_imports_per_day")

    assert status.limit is None and status.used is None and status.reached is False
    repo["count_imports_reached_allocation"].assert_not_awaited()


async def test_below_the_limit_is_not_reached(repo):
    repo["get_plan_limit"].return_value = 5
    repo["count_active_accounts"].return_value = 4

    status = await check_limit(object(), USER, "accounts_max")

    assert (status.used, status.limit, status.reached) == (4, 5, False)
    assert status.resets_at is None
    repo["count_active_accounts"].assert_awaited_once()


async def test_ai_proposals_count_the_days_model_calls(repo):
    # ab-314: every model call recorded in ai_layout_proposals that EAT day.
    repo["get_plan_limit"].return_value = 5
    repo["count_proposals"].return_value = 5
    status = await check_limit(object(), USER, "ai_layout_proposals_per_day")
    assert (status.used, status.limit, status.reached) == (5, 5, True)
    assert status.resets_at is not None
    start, end = repo["count_proposals"].await_args.kwargs["start"], repo["count_proposals"].await_args.kwargs["end"]
    assert end - start == timedelta(days=1)


async def test_an_unknown_key_is_a_programming_error(repo):
    with pytest.raises(ValueError):
        await check_limit(object(), USER, "nope")


# --- enforce_limit ----------------------------------------------------------


async def test_enforce_passes_below_the_limit(repo):
    repo["get_plan_limit"].return_value = 5
    repo["count_active_accounts"].return_value = 4
    await enforce_limit(object(), USER, "accounts_max")


async def test_enforce_passes_when_unlimited(repo):
    await enforce_limit(object(), USER, "accounts_max")


async def test_at_the_limit_raises_the_contract_body(repo):
    repo["get_plan_limit"].return_value = 5
    repo["count_imports_reached_allocation"].return_value = 5

    with pytest.raises(LimitReached) as exc:
        await enforce_limit(object(), USER, "statement_imports_per_day",
                            now=datetime(2026, 11, 24, 10, 0, tzinfo=EAT))

    assert exc.value.status_code == 429
    assert exc.value.detail == {
        "code": "limit_reached", "limit_key": "statement_imports_per_day", "used": 5, "limit": 5,
        "resets_at": "2026-11-25T00:00:00+03:00",
    }


async def test_over_a_lowered_limit_still_cant_add_more(repo):
    repo["get_plan_limit"].return_value = 2
    repo["count_active_accounts"].return_value = 7

    with pytest.raises(LimitReached) as exc:
        await enforce_limit(object(), USER, "accounts_max")

    assert exc.value.detail == {"code": "limit_reached", "limit_key": "accounts_max", "used": 7, "limit": 2,
                                "resets_at": None}


async def test_a_zero_limit_blocks_everything(repo):
    repo["get_plan_limit"].return_value = 0
    with pytest.raises(LimitReached):
        await enforce_limit(object(), USER, "accounts_max")
