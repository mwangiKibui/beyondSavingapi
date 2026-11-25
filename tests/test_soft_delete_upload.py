"""ab-334: deleting an upload is a soft delete of the upload and of the
transactions it added, in one transaction. `transactions` and
`statement_imports` are views over live rows (beyondSavingdb 000041), so a
deleted record drops out of every query. SQL runs against a recording
fake; the live check runs it against Postgres."""

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from app.repositories.statement_imports import ImportStillProcessing, soft_delete_import

IMPORT_ID = uuid4()


class FakeConn:
    def __init__(self, log, *, status="parsed", removed=1040, fail_on=None):
        self.log = log
        self.status = status
        self.removed = removed
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
            return self.status
        raise AssertionError(f"unexpected fetchval: {sql}")

    async def execute(self, sql, *args):
        self.log.append(("execute", sql, args))
        self._maybe_fail(sql)
        if "UPDATE transactions SET deleted_at" in sql:
            return f"UPDATE {self.removed}"
        return "UPDATE 0"


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
        elif "SET transfer_group_id = NULL" in entry[1]:
            out.append("unpair")
        elif "UPDATE transactions SET deleted_at" in entry[1]:
            out.append("rows")
        elif "UPDATE statement_imports SET deleted_at" in entry[1]:
            out.append("upload")
    return out


def sql_of(pool, step):
    return next(e[1] for e in pool.log if e[0] not in ("begin", "commit", "rollback") and step in e[1])


async def test_the_upload_and_its_rows_are_soft_deleted_in_one_transaction():
    pool = FakePool()
    assert await soft_delete_import(pool, import_id=IMPORT_ID) == 1040
    assert steps(pool) == ["begin", "lock", "move", "unpair", "rows", "upload", "commit"]
    for entry in pool.log:
        if entry[0] not in ("begin", "commit", "rollback"):
            assert entry[2] == (IMPORT_ID,)


async def test_nothing_is_ever_hard_deleted():
    pool = FakePool()
    await soft_delete_import(pool, import_id=IMPORT_ID)
    assert not any("DELETE" in e[1] for e in pool.log if e[0] not in ("begin", "commit", "rollback"))
    assert "deleted_at = now()" in sql_of(pool, "UPDATE transactions SET deleted_at")
    assert "WHERE import_id = $1" in sql_of(pool, "UPDATE transactions SET deleted_at")
    assert "WHERE id = $1" in sql_of(pool, "UPDATE statement_imports SET deleted_at")


async def test_an_upload_that_added_nothing_is_soft_deleted_too():
    pool = FakePool(removed=0)
    assert await soft_delete_import(pool, import_id=IMPORT_ID) == 0
    assert steps(pool)[-2:] == ["upload", "commit"]


async def test_a_live_transfer_paired_with_a_deleted_row_is_unpaired():
    pool = FakePool()
    await soft_delete_import(pool, import_id=IMPORT_ID)
    sql = " ".join(sql_of(pool, "SET transfer_group_id = NULL").split())
    # Only the other side of a pair whose row is being deleted; the
    # deleted rows themselves are left as they were.
    assert "transfer_group_id IN (SELECT transfer_group_id FROM transactions WHERE import_id = $1" in sql
    assert "import_id IS DISTINCT FROM $1" in sql


async def test_an_upload_that_is_still_processing_is_kept():
    pool = FakePool(status="pending")
    with pytest.raises(ImportStillProcessing):
        await soft_delete_import(pool, import_id=IMPORT_ID)
    assert steps(pool) == ["begin", "lock", "rollback"]


async def test_an_upload_that_is_already_gone_returns_none():
    pool = FakePool(status=None)
    assert await soft_delete_import(pool, import_id=IMPORT_ID) is None
    assert steps(pool) == ["begin", "lock", "commit"]


async def test_a_failure_rolls_everything_back():
    pool = FakePool(fail_on="UPDATE statement_imports SET deleted_at")
    with pytest.raises(RuntimeError):
        await soft_delete_import(pool, import_id=IMPORT_ID)
    assert steps(pool) == ["begin", "lock", "move", "unpair", "rows", "upload", "rollback"]


# --- the rest of the API around the views -----------------------------------------


async def test_duplicates_are_only_checked_among_live_rows():
    """A deleted statement can be uploaded again: the insert names the
    live-rows unique index (transactions_live_dedupe)."""
    import inspect

    from app.repositories import transactions

    source = inspect.getsource(transactions)
    assert "ON CONFLICT (account_id, dedupe_hash) WHERE deleted_at IS NULL DO NOTHING" in source
    assert "ON CONFLICT (account_id, dedupe_hash) DO NOTHING" not in source


async def test_the_uploads_list_says_how_many_rows_are_allocated():
    import inspect

    from app.repositories import statement_imports

    source = " ".join(inspect.getsource(statement_imports.list_statement_imports).split())
    assert "AS allocated_count" in source
    assert "EXISTS (SELECT 1 FROM allocations al WHERE al.transaction_id = t.id)" in source


async def test_notifications_of_a_deleted_upload_are_not_shown():
    import inspect

    from app.repositories import notifications

    assert notifications._LIVE_UPLOAD == "(import_id IS NULL OR import_id IN (SELECT id FROM statement_imports))"
    source = inspect.getsource(notifications.list_notifications)
    # Both the page and the unread count.
    assert source.count("{_LIVE_UPLOAD}") == 2
