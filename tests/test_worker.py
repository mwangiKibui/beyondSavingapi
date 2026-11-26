import logging
from datetime import date
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.parsers.base import StatementParseError
from app.worker import process_job, run_worker


def _txn(*, txn_date=date(2026, 1, 1), amount=100.0, direction="in", description="test") -> dict:
    return {
        "txn_date": txn_date,
        "amount": amount,
        "currency": "KES",
        "direction": direction,
        "counterparty": None,
        "description": description,
        "balance_after": amount,
        "dedupe_hash": f"hash-{description}",
    }


@pytest.fixture
def fake_pool():
    return object()


@pytest.fixture(autouse=True)
def _no_profiles(monkeypatch):
    """ab-312: no stored profiles unless a test adds some - the bespoke
    parser tests below run exactly as before."""
    monkeypatch.setattr("app.worker.list_active_profiles", AsyncMock(return_value=[]))


async def test_process_job_skips_unknown_import(fake_pool, monkeypatch, caplog):
    monkeypatch.setattr("app.worker.get_import_for_processing", AsyncMock(return_value=None))
    mock_mark_failed = AsyncMock()
    monkeypatch.setattr("app.worker.mark_import_failed", mock_mark_failed)

    with caplog.at_level(logging.WARNING):
        await process_job(fake_pool, str(uuid4()))

    mock_mark_failed.assert_not_called()
    assert any("unknown import" in r.message for r in caplog.records)


async def test_process_job_logs_and_returns_on_malformed_import_id(fake_pool, monkeypatch, caplog):
    mock_get_import = AsyncMock()
    monkeypatch.setattr("app.worker.get_import_for_processing", mock_get_import)
    mock_mark_failed = AsyncMock()
    monkeypatch.setattr("app.worker.mark_import_failed", mock_mark_failed)

    with caplog.at_level(logging.ERROR):
        await process_job(fake_pool, "not-a-uuid")

    mock_get_import.assert_not_called()
    mock_mark_failed.assert_not_called()
    assert any("malformed import id" in r.message for r in caplog.records)


async def test_process_job_fails_a_file_without_text(fake_pool, monkeypatch, caplog):
    import_id = uuid4()
    monkeypatch.setattr(
        "app.worker.get_import_for_processing",
        AsyncMock(
            return_value={
                "id": import_id,
                "account_id": uuid4(),
                "storage_key": "statements/x/statement.pdf",
                "file_name": "statement.pdf",
                "provider": "M-Pesa",
            }
        ),
    )
    mock_mark_failed = AsyncMock()
    monkeypatch.setattr("app.worker.mark_import_failed", mock_mark_failed)
    monkeypatch.setattr("app.worker.PARSERS", {})
    mock_storage_client = MagicMock()
    mock_storage_client.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=b"not a pdf"))}
    monkeypatch.setattr("app.worker.get_storage_client", lambda: mock_storage_client)

    with caplog.at_level(logging.INFO):
        await process_job(fake_pool, str(import_id))

    assert "couldn't find any text" in mock_mark_failed.call_args.kwargs["error_detail"]
    assert any("not read" in r.message for r in caplog.records)


async def test_process_job_fetches_content_and_calls_a_registered_parser(fake_pool, monkeypatch):
    import_id = uuid4()
    account_id = uuid4()
    monkeypatch.setattr(
        "app.worker.get_import_for_processing",
        AsyncMock(
            return_value={
                "id": import_id,
                "account_id": account_id,
                "storage_key": "statements/x/statement.pdf",
                "file_name": "statement.pdf",
                "provider": "Test Provider",
            }
        ),
    )
    mock_mark_failed = AsyncMock()
    monkeypatch.setattr("app.worker.mark_import_failed", mock_mark_failed)

    mock_parser = MagicMock(return_value=[])
    monkeypatch.setattr("app.worker.PARSERS", {"Test Provider": mock_parser})

    mock_insert = AsyncMock(return_value=0)
    monkeypatch.setattr("app.worker.insert_transactions", mock_insert)
    mock_mark_parsed = AsyncMock()
    monkeypatch.setattr("app.worker.mark_import_parsed", mock_mark_parsed)

    mock_body = MagicMock()
    mock_body.read.return_value = b"file content"
    mock_storage_client = MagicMock()
    mock_storage_client.get_object.return_value = {"Body": mock_body}
    monkeypatch.setattr("app.worker.get_storage_client", lambda: mock_storage_client)

    await process_job(fake_pool, str(import_id))

    mock_storage_client.get_object.assert_called_once_with(
        Bucket="statements", Key="statements/x/statement.pdf"
    )
    mock_parser.assert_called_once_with(b"file content")
    mock_insert.assert_called_once_with(fake_pool, account_id=account_id, import_id=import_id, transactions=[])
    mock_mark_parsed.assert_called_once_with(
        fake_pool,
        import_id=import_id,
        period_start=None,
        period_end=None,
        row_count=0,
        opening_balance=None,
        opening_balance_date=None,
    )
    mock_mark_failed.assert_not_called()


async def test_process_job_writes_transactions_and_marks_parsed_with_real_period_and_row_count(
    fake_pool, monkeypatch
):
    import_id = uuid4()
    account_id = uuid4()
    monkeypatch.setattr(
        "app.worker.get_import_for_processing",
        AsyncMock(
            return_value={
                "id": import_id,
                "account_id": account_id,
                "storage_key": "statements/x/statement.pdf",
                "file_name": "statement.pdf",
                "provider": "Test Provider",
            }
        ),
    )
    transactions = [
        _txn(txn_date=date(2026, 1, 5), description="first"),
        _txn(txn_date=date(2026, 1, 20), description="second"),
    ]
    monkeypatch.setattr("app.worker.PARSERS", {"Test Provider": MagicMock(return_value=transactions)})

    # One of the two collided with an already-imported duplicate.
    mock_insert = AsyncMock(return_value=1)
    monkeypatch.setattr("app.worker.insert_transactions", mock_insert)
    mock_mark_parsed = AsyncMock()
    monkeypatch.setattr("app.worker.mark_import_parsed", mock_mark_parsed)
    monkeypatch.setattr("app.worker.mark_import_failed", AsyncMock())

    mock_body = MagicMock()
    mock_body.read.return_value = b"file content"
    mock_storage_client = MagicMock()
    mock_storage_client.get_object.return_value = {"Body": mock_body}
    monkeypatch.setattr("app.worker.get_storage_client", lambda: mock_storage_client)

    await process_job(fake_pool, str(import_id))

    mock_insert.assert_called_once_with(
        fake_pool, account_id=account_id, import_id=import_id, transactions=transactions
    )
    # row_count reflects everything the parser found (matches
    # docs/schema.sql's own "transactions parsed from this file"
    # comment), not just what survived the dedupe skip.
    # opening_balance/opening_balance_date: derived from the oldest
    # transaction (_txn defaults balance_after to its own amount), not
    # re-parsed - see derive_opening_balance.
    mock_mark_parsed.assert_called_once_with(
        fake_pool,
        import_id=import_id,
        period_start=date(2026, 1, 5),
        period_end=date(2026, 1, 20),
        row_count=2,
        opening_balance=0.0,
        opening_balance_date=date(2026, 1, 5),
    )


async def test_process_job_routes_mentor_sacco_sections_to_their_selected_sub_ledgers(fake_pool, monkeypatch):
    import_id = uuid4()
    account_id = uuid4()  # ONE account - both sub-ledgers attach to it (ab-119)
    ordinary_deposit_id = uuid4()
    savings_account_id = uuid4()
    monkeypatch.setattr(
        "app.worker.get_import_for_processing",
        AsyncMock(
            return_value={
                "id": import_id,
                "account_id": account_id,
                "storage_key": "statements/x/statement.pdf",
                "file_name": "statement.pdf",
                "provider": "Mentor Sacco",
            }
        ),
    )
    sections = [
        {
            "name": "Ordinary Deposit",
            "opening_balance": 0.0,
            "transactions": [_txn(txn_date=date(2026, 1, 5), description="od-1")],
        },
        {
            "name": "Savings Account",
            "opening_balance": 0.0,
            "transactions": [_txn(txn_date=date(2026, 1, 10), description="sav-1")],
        },
    ]
    monkeypatch.setattr("app.worker.parse_mentor_sacco_statement", MagicMock(return_value=sections))
    monkeypatch.setattr(
        "app.worker.get_import_sub_ledgers",
        AsyncMock(
            return_value=[
                {"id": ordinary_deposit_id, "name": "Ordinary Deposit"},
                {"id": savings_account_id, "name": "Savings Account"},
            ]
        ),
    )
    mock_insert = AsyncMock(return_value=1)
    monkeypatch.setattr("app.worker.insert_transactions", mock_insert)
    mock_mark_parsed = AsyncMock()
    monkeypatch.setattr("app.worker.mark_import_parsed", mock_mark_parsed)
    monkeypatch.setattr("app.worker.mark_import_failed", AsyncMock())
    mock_set_opening_balances = AsyncMock()
    monkeypatch.setattr("app.worker.set_sub_ledger_opening_balances", mock_set_opening_balances)

    mock_body = MagicMock()
    mock_body.read.return_value = b"file content"
    mock_storage_client = MagicMock()
    mock_storage_client.get_object.return_value = {"Body": mock_body}
    monkeypatch.setattr("app.worker.get_storage_client", lambda: mock_storage_client)

    await process_job(fake_pool, str(import_id))

    assert mock_insert.call_args_list == [
        (
            (fake_pool,),
            {
                "account_id": account_id,
                "import_id": import_id,
                "transactions": sections[0]["transactions"],
                "sub_ledger_id": ordinary_deposit_id,
            },
        ),
        (
            (fake_pool,),
            {
                "account_id": account_id,
                "import_id": import_id,
                "transactions": sections[1]["transactions"],
                "sub_ledger_id": savings_account_id,
            },
        ),
    ]
    mock_mark_parsed.assert_called_once_with(
        fake_pool,
        import_id=import_id,
        period_start=date(2026, 1, 5),
        period_end=date(2026, 1, 10),
        row_count=2,
    )
    # Each sub-ledger's opening balance is derived from its OWN section's
    # oldest transaction (_txn defaults balance_after to its own amount,
    # direction "in"), not the combined/account-level list.
    mock_set_opening_balances.assert_called_once_with(
        fake_pool,
        import_id=import_id,
        opening_balances=[
            (ordinary_deposit_id, 0.0, date(2026, 1, 5)),
            (savings_account_id, 0.0, date(2026, 1, 10)),
        ],
    )


async def test_process_job_merges_both_instant_loan_instances_into_one_sub_ledger(fake_pool, monkeypatch):
    # "Instant Loan" can appear twice in one statement as two genuinely
    # separate balance sequences (ab-115) but there's only ever one
    # "Instant Loan" sub-ledger - both instances' transactions must
    # insert against that same sub_ledger_id.
    import_id = uuid4()
    account_id = uuid4()
    instant_loan_id = uuid4()
    monkeypatch.setattr(
        "app.worker.get_import_for_processing",
        AsyncMock(
            return_value={
                "id": import_id,
                "account_id": account_id,
                "storage_key": "statements/x/statement.pdf",
                "file_name": "statement.pdf",
                "provider": "Mentor Sacco",
            }
        ),
    )
    sections = [
        {"name": "Instant Loan", "opening_balance": 0.0, "transactions": [_txn(description="loan-a")]},
        {"name": "Instant Loan", "opening_balance": 0.0, "transactions": [_txn(description="loan-b")]},
    ]
    monkeypatch.setattr("app.worker.parse_mentor_sacco_statement", MagicMock(return_value=sections))
    monkeypatch.setattr(
        "app.worker.get_import_sub_ledgers",
        AsyncMock(return_value=[{"id": instant_loan_id, "name": "Instant Loan"}]),
    )
    mock_insert = AsyncMock(return_value=2)
    monkeypatch.setattr("app.worker.insert_transactions", mock_insert)
    monkeypatch.setattr("app.worker.mark_import_parsed", AsyncMock())
    monkeypatch.setattr("app.worker.mark_import_failed", AsyncMock())
    monkeypatch.setattr("app.worker.set_sub_ledger_opening_balances", AsyncMock())

    mock_body = MagicMock()
    mock_body.read.return_value = b"file content"
    mock_storage_client = MagicMock()
    mock_storage_client.get_object.return_value = {"Body": mock_body}
    monkeypatch.setattr("app.worker.get_storage_client", lambda: mock_storage_client)

    await process_job(fake_pool, str(import_id))

    mock_insert.assert_called_once_with(
        fake_pool,
        account_id=account_id,
        import_id=import_id,
        transactions=sections[0]["transactions"] + sections[1]["transactions"],
        sub_ledger_id=instant_loan_id,
    )


async def test_process_job_fails_cleanly_when_no_sub_ledgers_were_selected(fake_pool, monkeypatch):
    import_id = uuid4()
    monkeypatch.setattr(
        "app.worker.get_import_for_processing",
        AsyncMock(
            return_value={
                "id": import_id,
                "account_id": uuid4(),
                "storage_key": "statements/x/statement.pdf",
                "file_name": "statement.pdf",
                "provider": "Mentor Sacco",
            }
        ),
    )
    monkeypatch.setattr(
        "app.worker.parse_mentor_sacco_statement",
        MagicMock(return_value=[{"name": "Ordinary Deposit", "opening_balance": 0.0, "transactions": [_txn()]}]),
    )
    monkeypatch.setattr("app.worker.get_import_sub_ledgers", AsyncMock(return_value=[]))
    mock_insert = AsyncMock()
    monkeypatch.setattr("app.worker.insert_transactions", mock_insert)
    mock_mark_failed = AsyncMock()
    monkeypatch.setattr("app.worker.mark_import_failed", mock_mark_failed)

    mock_body = MagicMock()
    mock_body.read.return_value = b"file content"
    mock_storage_client = MagicMock()
    mock_storage_client.get_object.return_value = {"Body": mock_body}
    monkeypatch.setattr("app.worker.get_storage_client", lambda: mock_storage_client)

    await process_job(fake_pool, str(import_id))

    mock_insert.assert_not_called()
    mock_mark_failed.assert_called_once_with(
        fake_pool,
        import_id=import_id,
        error_detail="No sub-ledgers were selected for this upload - choose at least one to import.",
    )


async def test_process_job_fails_cleanly_when_a_selected_sub_ledger_has_no_matching_section(
    fake_pool, monkeypatch
):
    import_id = uuid4()
    monkeypatch.setattr(
        "app.worker.get_import_for_processing",
        AsyncMock(
            return_value={
                "id": import_id,
                "account_id": uuid4(),
                "storage_key": "statements/x/statement.pdf",
                "file_name": "statement.pdf",
                "provider": "Mentor Sacco",
            }
        ),
    )
    # Statement only has Ordinary Deposit, but the user selected Share
    # Capital too (e.g. a typo'd sub-ledger name).
    monkeypatch.setattr(
        "app.worker.parse_mentor_sacco_statement",
        MagicMock(return_value=[{"name": "Ordinary Deposit", "opening_balance": 0.0, "transactions": [_txn()]}]),
    )
    monkeypatch.setattr(
        "app.worker.get_import_sub_ledgers",
        AsyncMock(
            return_value=[
                {"id": uuid4(), "name": "Ordinary Deposit"},
                {"id": uuid4(), "name": "Share Capital"},
            ]
        ),
    )
    mock_insert = AsyncMock()
    monkeypatch.setattr("app.worker.insert_transactions", mock_insert)
    mock_mark_failed = AsyncMock()
    monkeypatch.setattr("app.worker.mark_import_failed", mock_mark_failed)

    mock_body = MagicMock()
    mock_body.read.return_value = b"file content"
    mock_storage_client = MagicMock()
    mock_storage_client.get_object.return_value = {"Body": mock_body}
    monkeypatch.setattr("app.worker.get_storage_client", lambda: mock_storage_client)

    await process_job(fake_pool, str(import_id))

    # Nothing written at all - not even Ordinary Deposit, whose section
    # does exist - since every selected sub-ledger is resolved before
    # any insert.
    mock_insert.assert_not_called()
    mock_mark_failed.assert_called_once_with(
        fake_pool,
        import_id=import_id,
        error_detail="No section named 'Share Capital' was found in this statement - "
        "check the sub-ledger's name matches the statement exactly.",
    )


async def test_process_job_marks_failed_with_the_parsers_own_message_on_statement_parse_error(
    fake_pool, monkeypatch, caplog
):
    import_id = uuid4()
    monkeypatch.setattr(
        "app.worker.get_import_for_processing",
        AsyncMock(
            return_value={
                "id": import_id,
                "account_id": uuid4(),
                "storage_key": "statements/x/statement.pdf",
                "file_name": "statement.pdf",
                "provider": "Test Provider",
            }
        ),
    )
    mock_mark_failed = AsyncMock()
    monkeypatch.setattr("app.worker.mark_import_failed", mock_mark_failed)

    mock_parser = MagicMock(side_effect=StatementParseError("Running balance mismatch at transaction 3"))
    monkeypatch.setattr("app.worker.PARSERS", {"Test Provider": mock_parser})

    mock_body = MagicMock()
    mock_body.read.return_value = b"file content"
    mock_storage_client = MagicMock()
    mock_storage_client.get_object.return_value = {"Body": mock_body}
    monkeypatch.setattr("app.worker.get_storage_client", lambda: mock_storage_client)

    with caplog.at_level(logging.INFO):
        await process_job(fake_pool, str(import_id))

    mock_mark_failed.assert_called_once_with(
        fake_pool, import_id=import_id, error_detail="Running balance mismatch at transaction 3"
    )
    assert any("rejected" in r.message for r in caplog.records)


async def test_process_job_marks_failed_with_generic_message_on_unexpected_error(fake_pool, monkeypatch, caplog):
    import_id = uuid4()
    monkeypatch.setattr(
        "app.worker.get_import_for_processing",
        AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz")),
    )
    mock_mark_failed = AsyncMock()
    monkeypatch.setattr("app.worker.mark_import_failed", mock_mark_failed)

    with caplog.at_level(logging.ERROR):
        await process_job(fake_pool, str(import_id))

    mock_mark_failed.assert_called_once_with(
        fake_pool,
        import_id=import_id,
        error_detail="Something went wrong while processing this statement.",
    )
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    assert "secret=xyz" not in mock_mark_failed.call_args.kwargs["error_detail"]


async def test_process_job_logs_but_does_not_raise_if_marking_failed_also_fails(fake_pool, monkeypatch, caplog):
    monkeypatch.setattr(
        "app.worker.get_import_for_processing", AsyncMock(side_effect=RuntimeError("db down"))
    )
    monkeypatch.setattr(
        "app.worker.mark_import_failed", AsyncMock(side_effect=RuntimeError("db still down"))
    )

    with caplog.at_level(logging.ERROR):
        await process_job(fake_pool, str(uuid4()))  # must not raise

    assert any("Additionally failed to mark import" in r.message for r in caplog.records)


async def test_run_worker_processes_each_popped_job(fake_pool, monkeypatch):
    mock_redis = MagicMock()
    mock_redis.brpop = AsyncMock(
        side_effect=[
            None,
            ("parse_jobs", "job-1"),
            ("parse_jobs", "job-2"),
            RuntimeError("stop the loop"),
        ]
    )
    monkeypatch.setattr("app.worker.get_redis", lambda: mock_redis)
    mock_process_job = AsyncMock()
    monkeypatch.setattr("app.worker.process_job", mock_process_job)

    with pytest.raises(RuntimeError, match="stop the loop"):
        await run_worker(fake_pool)

    assert mock_process_job.call_args_list == [
        ((fake_pool, "job-1"),),
        ((fake_pool, "job-2"),),
    ]


# --- ab-281: rows handled right after parsing --------------------------------

def _parsed_job(monkeypatch, import_id):
    monkeypatch.setattr(
        "app.worker.get_import_for_processing",
        AsyncMock(return_value={"id": import_id, "account_id": uuid4(), "storage_key": "k",
                                "file_name": "s.pdf", "provider": "Test Provider"}),
    )
    monkeypatch.setattr("app.worker.PARSERS", {"Test Provider": MagicMock(return_value=[])})
    monkeypatch.setattr("app.worker.insert_transactions", AsyncMock(return_value=0))
    monkeypatch.setattr("app.worker.mark_import_parsed", AsyncMock())
    mark_failed = AsyncMock()
    monkeypatch.setattr("app.worker.mark_import_failed", mark_failed)
    body = MagicMock()
    body.read.return_value = b"x"
    storage = MagicMock()
    storage.get_object.return_value = {"Body": body}
    monkeypatch.setattr("app.worker.get_storage_client", lambda: storage)
    return mark_failed


async def test_process_job_handles_rows_after_parsing(fake_pool, monkeypatch):
    import_id = uuid4()
    mark_failed = _parsed_job(monkeypatch, import_id)
    handle = AsyncMock(return_value=None)
    monkeypatch.setattr("app.worker.handle_import_by_id", handle)

    await process_job(fake_pool, str(import_id))

    assert handle.call_args.kwargs["import_id"] == import_id
    mark_failed.assert_not_called()


async def test_a_handling_failure_never_fails_the_import(fake_pool, monkeypatch, caplog):
    import_id = uuid4()
    mark_failed = _parsed_job(monkeypatch, import_id)
    monkeypatch.setattr("app.worker.handle_import_by_id", AsyncMock(side_effect=RuntimeError("db down")))

    with caplog.at_level(logging.ERROR):
        await process_job(fake_pool, str(import_id))

    mark_failed.assert_not_called()
    assert "will retry when its review opens" in caplog.text


async def test_ab336_ai_lookups_run_beside_parse_jobs(fake_pool, monkeypatch):
    import asyncio

    mock_redis = MagicMock()
    iid = uuid4()
    mock_redis.brpop = AsyncMock(
        side_effect=[("ai_category_jobs", str(iid)), ("parse_jobs", "job-1"), RuntimeError("stop the loop")]
    )
    monkeypatch.setattr("app.worker.get_redis", lambda: mock_redis)
    monkeypatch.setattr("app.worker.process_job", AsyncMock())
    lookup = AsyncMock()
    monkeypatch.setattr("app.worker.run_lookup", lookup)

    with pytest.raises(RuntimeError, match="stop the loop"):
        await run_worker(fake_pool)
    await asyncio.sleep(0)

    assert mock_redis.brpop.await_args_list[0].args[0] == ["parse_jobs", "ai_category_jobs"]
    lookup.assert_awaited_once_with(fake_pool, iid)


async def test_ab338_the_ai_lookup_is_flagged_before_the_upload_is_marked_parsed(fake_pool, monkeypatch):
    """A page polling the upload must never see it parsed before the
    lookup shows as running (the 45-then-345 race)."""
    import_id = uuid4()
    monkeypatch.setattr(
        "app.worker.get_import_for_processing",
        AsyncMock(return_value={"id": import_id, "account_id": uuid4(), "storage_key": "k",
                                "file_name": "statement.pdf", "provider": "Test Provider"}),
    )
    monkeypatch.setattr("app.worker.PARSERS", {"Test Provider": MagicMock(return_value=[])})
    monkeypatch.setattr("app.worker.insert_transactions", AsyncMock(return_value=0))
    order = []
    monkeypatch.setattr("app.worker.begin_lookup", AsyncMock(side_effect=lambda i: order.append("begin") or True))
    monkeypatch.setattr("app.worker.mark_import_parsed", AsyncMock(side_effect=lambda *a, **k: order.append("parsed")))
    monkeypatch.setattr("app.worker.queue_lookup",
                        AsyncMock(side_effect=lambda i, **k: order.append(("queue", k)) or True))
    monkeypatch.setattr("app.worker.cancel_lookup", AsyncMock(side_effect=lambda i: order.append("cancel")))
    body = MagicMock()
    body.read.return_value = b"x"
    storage = MagicMock()
    storage.get_object.return_value = {"Body": body}
    monkeypatch.setattr("app.worker.get_storage_client", lambda: storage)

    await process_job(fake_pool, str(import_id))

    assert order == ["begin", "parsed", ("queue", {"fresh": True, "begun": True})]


async def test_ab338_an_upload_that_is_not_parsed_drops_its_lookup_flag(fake_pool, monkeypatch):
    import_id = uuid4()
    monkeypatch.setattr(
        "app.worker.get_import_for_processing",
        AsyncMock(return_value={"id": import_id, "account_id": uuid4(), "storage_key": "k",
                                "file_name": "statement.pdf", "provider": "Test Provider"}),
    )
    from app.parsers.base import StatementParseError

    monkeypatch.setattr("app.worker.PARSERS", {"Test Provider": MagicMock(side_effect=StatementParseError("bad"))})
    monkeypatch.setattr("app.worker.mark_import_failed", AsyncMock())
    monkeypatch.setattr("app.worker.begin_lookup", AsyncMock(return_value=True))
    queue = AsyncMock()
    monkeypatch.setattr("app.worker.queue_lookup", queue)
    cancel = AsyncMock()
    monkeypatch.setattr("app.worker.cancel_lookup", cancel)
    body = MagicMock()
    body.read.return_value = b"x"
    storage = MagicMock()
    storage.get_object.return_value = {"Body": body}
    monkeypatch.setattr("app.worker.get_storage_client", lambda: storage)

    await process_job(fake_pool, str(import_id))

    queue.assert_not_called()
    cancel.assert_awaited_once_with(import_id)
