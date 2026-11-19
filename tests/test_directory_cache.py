"""ab-300: the shared payee Directory is built once per process and rebuilt
only when its source tables change (or after a max age backstop)."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import app.services.directory_cache as cache
from app.repositories.category_suggestions import get_directory_version
from app.services.category_suggestions import Directory, build_preview_rows, extract_keys

MOD = "app.services.directory_cache"
NAIVAS = {"merchant_id": uuid4(), "display_name": "Naivas", "category_name": "Groceries", "direction": "out",
          "key_kind": "name_pattern", "key_value": "NAIVAS", "requires_text": False}
V1 = (1, 1, "t1", 1, "t1", 0, None)


@pytest.fixture
def reads(monkeypatch):
    mocks = {
        "get_directory_version": AsyncMock(return_value=V1),
        "get_approved_directory": AsyncMock(return_value=[NAIVAS]),
        "get_blocked_numbers": AsyncMock(return_value=[]),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(f"{MOD}.{name}", mock)
    return mocks


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(f"{MOD}.time.monotonic", lambda: now["t"])
    return now


async def test_first_call_builds_from_the_rows(reads):
    d = await cache.get_directory(object())
    assert isinstance(d, Directory)
    assert d.match_text("NAIVAS WESTLANDS", "out").display_name == "Naivas"
    reads["get_approved_directory"].assert_awaited_once()
    reads["get_blocked_numbers"].assert_awaited_once()


async def test_second_call_with_same_version_reuses_the_built_directory(reads, monkeypatch):
    builds = []
    real = Directory.from_rows.__func__

    def counting(cls, rows, *, blocked):
        builds.append(1)
        return real(cls, rows, blocked=blocked)

    monkeypatch.setattr(Directory, "from_rows", classmethod(counting))
    first = await cache.get_directory(object())
    second = await cache.get_directory(object())
    assert second is first
    assert len(builds) == 1
    assert reads["get_approved_directory"].await_count == 1
    assert reads["get_directory_version"].await_count == 2


@pytest.mark.parametrize("changed", [
    (2, 2, "t1", 1, "t1", 0, None),    # merchant added
    (1, 1, "t2", 1, "t1", 0, None),    # merchant edited (updated_at)
    (0, 0, "t1", 0, "t1", 0, None),    # merchant removed (keys cascade)
    (1, 1, "t1", 1, "t2", 0, None),    # keys replaced
    (1, 1, "t1", 2, "t2", 0, None),    # a user-taught number key added
    (1, 1, "t1", 1, "t1", 1, "t2"),    # blocked number added
    (0, 1, "t1", 1, "t1", 0, None),    # status flipped away from approved
])
async def test_each_change_rebuilds(reads, changed):
    first = await cache.get_directory(object())
    reads["get_directory_version"].return_value = changed
    second = await cache.get_directory(object())
    assert second is not first
    assert reads["get_approved_directory"].await_count == 2


async def test_blocked_number_removed_rebuilds(reads):
    reads["get_directory_version"].return_value = (1, 1, "t1", 1, "t1", 1, "t1")
    reads["get_blocked_numbers"].return_value = [{"number_kind": "paybill", "number": "222222"}]
    first = await cache.get_directory(object())
    assert first.is_blocked("paybill", "222222")
    reads["get_directory_version"].return_value = (1, 1, "t1", 1, "t1", 0, None)
    reads["get_blocked_numbers"].return_value = []
    second = await cache.get_directory(object())
    assert not second.is_blocked("paybill", "222222")


async def test_rebuilds_after_max_age_even_with_same_version(reads, clock):
    first = await cache.get_directory(object())
    clock["t"] += cache.MAX_AGE_SECONDS - 1
    assert await cache.get_directory(object()) is first
    clock["t"] += 2
    assert await cache.get_directory(object()) is not first


async def test_cached_under_the_version_read_before_the_rows(reads):
    """A write landing between the version read and the row reads must not
    be hidden: the next call sees a newer version and rebuilds."""
    v2 = (2, 2, "t2", 2, "t2", 0, None)

    async def rows_then_write(conn):
        reads["get_directory_version"].return_value = v2
        return [NAIVAS]

    reads["get_approved_directory"].side_effect = rows_then_write
    await cache.get_directory(object())
    await cache.get_directory(object())
    assert reads["get_approved_directory"].await_count == 2


async def test_clear_forces_a_rebuild(reads):
    first = await cache.get_directory(object())
    cache.clear()
    assert await cache.get_directory(object()) is not first


async def test_suggestions_identical_to_building_each_time(reads):
    rows = [{"id": uuid4(), "txn_date": "2026-10-01", "amount": 500.0, "currency": "KES", "direction": "out",
             "description": "Merchant Payment to 5551234 - NAIVAS WESTLANDS", "is_transfer": False,
             "allocated": 0, "category_id": None}]
    keys = extract_keys(rows, provider="M-Pesa", secret="s")
    groceries = [{"id": uuid4(), "name": "Groceries", "type": "expense", "entry_type": None}]

    def preview(directory):
        return build_preview_rows(rows, keys=keys, rules=[], directory=directory, categories=groceries)

    fresh = preview(Directory.from_rows([NAIVAS], blocked=[]))
    cached = preview(await cache.get_directory(object()))
    assert cached == fresh
    assert cached[0]["suggestion"] is not None


async def test_version_is_one_query_returning_a_hashable_tuple():
    conn = AsyncMock()
    conn.fetchrow.return_value = {"a": 1, "b": 2}
    version = await get_directory_version(conn)
    assert version == (1, 2)
    hash(version)
    conn.fetchrow.assert_awaited_once()
    sql = conn.fetchrow.call_args.args[0]
    for table in ("merchant_directory", "merchant_directory_keys", "blocked_numbers"):
        assert table in sql
