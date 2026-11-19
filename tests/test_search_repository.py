"""ab-274: the search SQL is always scoped to the user and pages correctly."""

import asyncio
from uuid import uuid4

import pytest

from app.repositories import search as repo


class FakeConn:
    def __init__(self, rows=(), count=0):
        self.rows, self.count, self.calls = list(rows), count, []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self.rows

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return self.count


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("call, scope", [
    (lambda c, u: repo.search_transactions(c, user_id=u, pattern="%x%", amount=None, limit=5, offset=0),
     "user_id = $1"),
    (lambda c, u: repo.search_payee_rows(c, user_id=u, pattern="%x%"), "user_id = $1"),
    (lambda c, u: repo.search_money_plans(c, user_id=u, pattern="%x%", limit=5, offset=0), "p.user_id = $1"),
    (lambda c, u: repo.search_accounts(c, user_id=u, pattern="%x%", limit=5, offset=0), "a.user_id = $1"),
])
def test_every_query_is_scoped_to_the_user(call, scope):
    conn, user_id = FakeConn(), uuid4()
    run(call(conn, user_id))
    sql, args = conn.calls[0]
    assert scope in sql and args[0] == user_id


def test_accounts_are_never_matched_on_their_number():
    conn = FakeConn()
    run(repo.search_accounts(conn, user_id=uuid4(), pattern="%1234%", limit=5, offset=0))
    assert "account_number" not in conn.calls[0][0]


def test_payee_rows_are_statement_rows_and_capped():
    conn = FakeConn()
    run(repo.search_payee_rows(conn, user_id=uuid4(), pattern="%x%"))
    sql, args = conn.calls[0]
    assert "t.source = 'statement'" in sql and args[-1] == repo.PAYEE_ROW_CAP


def test_a_page_carries_its_total():
    conn = FakeConn(rows=[{"id": 1, "total_count": 42}])
    rows, total = run(repo.search_money_plans(conn, user_id=uuid4(), pattern="%x%", limit=10, offset=20))
    assert total == 42 and len(rows) == 1
    assert conn.calls[0][1][-2:] == (10, 20)
    assert len(conn.calls) == 1


def test_a_page_past_the_end_still_reports_the_total():
    conn = FakeConn(rows=[], count=7)
    rows, total = run(repo.search_accounts(conn, user_id=uuid4(), pattern="%x%", limit=10, offset=20))
    assert (rows, total) == ([], 7)


def test_an_empty_first_page_needs_no_count():
    conn = FakeConn(rows=[], count=99)
    assert run(repo.search_transactions(conn, user_id=uuid4(), pattern="%x%", amount=None, limit=5,
                                        offset=0)) == ([], 0)
    assert len(conn.calls) == 1
