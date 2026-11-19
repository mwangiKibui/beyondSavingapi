"""ab-301: removing a duplicate upload moves its allocation batches
(category_applies) to the earlier upload that recorded the rows, in the
same transaction as the delete, so batch Undo stays in that upload's
review. SQL runs against a recording fake; the live check is manual."""

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from app.repositories.statement_imports import delete_import_without_rows, move_applies_to_earlier

IMPORT_ID = uuid4()


class FakeConn:
    def __init__(self, log, *, own_rows=False, moved=2, storage_key="statements/x/s.pdf", fail_on=None):
        self.log = log
        self.own_rows = own_rows
        self.moved = moved
        self.storage_key = storage_key
        self.fail_on = fail_on

    @asynccontextmanager
    async def transaction(self):
        self.log.append(("begin",))
        try:
            yield
        except Exception:
            self.log.append(("rollback",))
            raise
        self.log.append(("commit",))

    def _maybe_fail(self, sql):
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("db down")

    async def fetchval(self, sql, *args):
        self.log.append(("fetchval", sql, args))
        self._maybe_fail(sql)
        if "FOR UPDATE" in sql:
            return None if self.own_rows else IMPORT_ID
        if "DELETE FROM statement_imports" in sql:
            return self.storage_key
        raise AssertionError(f"unexpected fetchval: {sql}")

    async def execute(self, sql, *args):
        self.log.append(("execute", sql, args))
        self._maybe_fail(sql)
        return f"UPDATE {self.moved}"


class FakePool:
    def __init__(self, **kw):
        self.log = []
        self.conn = FakeConn(self.log, **kw)

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def steps(pool):
    out = []
    for entry in pool.log:
        if entry[0] in ("begin", "commit", "rollback"):
            out.append(entry[0])
        elif "FOR UPDATE" in entry[1]:
            out.append("lock")
        elif "UPDATE category_applies" in entry[1]:
            out.append("move")
        elif "DELETE FROM statement_imports" in entry[1]:
            out.append("delete")
    return out


def sql_of(pool, step):
    return next(e[1] for e in pool.log if e[0] not in ("begin", "commit", "rollback") and step in e[1])


async def test_a_duplicate_moves_its_applies_then_is_deleted_in_one_transaction():
    pool = FakePool()
    assert await delete_import_without_rows(pool, import_id=IMPORT_ID) == "statements/x/s.pdf"
    assert steps(pool) == ["begin", "lock", "move", "delete", "commit"]
    for entry in pool.log:
        if entry[0] not in ("begin", "commit", "rollback"):
            assert entry[2] == (IMPORT_ID,)


async def test_an_upload_with_its_own_rows_changes_nothing():
    pool = FakePool(own_rows=True)
    assert await delete_import_without_rows(pool, import_id=IMPORT_ID) is None
    assert steps(pool) == ["begin", "lock", "commit"]


async def test_the_lock_only_takes_an_import_without_rows_of_its_own():
    pool = FakePool()
    await delete_import_without_rows(pool, import_id=IMPORT_ID)
    lock = sql_of(pool, "FOR UPDATE")
    assert "NOT EXISTS (SELECT 1 FROM transactions t WHERE t.import_id = si.id)" in lock


async def test_a_failed_delete_rolls_the_move_back():
    pool = FakePool(fail_on="DELETE FROM statement_imports")
    with pytest.raises(RuntimeError):
        await delete_import_without_rows(pool, import_id=IMPORT_ID)
    assert steps(pool) == ["begin", "lock", "move", "delete", "rollback"]


async def test_moving_returns_how_many_applies_moved():
    pool = FakePool(moved=3)
    assert await move_applies_to_earlier(pool.conn, import_id=IMPORT_ID) == 3


async def test_each_apply_goes_to_the_upload_holding_most_of_its_rows():
    pool = FakePool()
    await move_applies_to_earlier(pool.conn, import_id=IMPORT_ID)
    sql = sql_of(pool, "UPDATE category_applies")
    # Routed by the rows the apply filed, on the same account, oldest upload on a tie.
    assert "JOIN allocations al ON al.apply_id = ca.id" in sql
    assert "e.account_id = si.account_id" in sql
    assert "ORDER BY al.apply_id, COUNT(*) DESC, e.created_at, t.import_id" in sql
    # Otherwise the upload the progress endpoint names as already_recorded_by.
    assert "COALESCE(b.import_id, (SELECT import_id FROM earlier))" in sql
    # No earlier upload: left alone, so the FK's SET NULL applies as before.
    assert "tg.import_id IS NOT NULL" in sql
    # Undone applies move too; only the duplicate's own applies are touched.
    assert "undone_at" not in sql
    assert "WHERE ca.import_id = $1" in sql


async def test_the_fallback_uses_the_same_rule_as_already_recorded_by(monkeypatch):
    from app.repositories import statement_imports as repo

    seen = []

    class OverlapPool:
        async def fetchrow(self, sql, *args):
            seen.append(sql)
            return {"added_count": 0, "already_count": 0, "earlier_id": None,
                    "earlier_file_name": None, "earlier_uploaded_at": None}

    await repo.get_import_overlap(OverlapPool(), import_id=IMPORT_ID)
    pool = FakePool()
    await move_applies_to_earlier(pool.conn, import_id=IMPORT_ID)
    assert repo._OVERLAP_CTES in seen[0]
    assert repo._OVERLAP_CTES in sql_of(pool, "UPDATE category_applies")
