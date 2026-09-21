import logging
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.parsers.base import StatementParseError
from app.worker import process_job, run_worker


@pytest.fixture
def fake_pool():
    return object()


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


async def test_process_job_marks_failed_when_no_parser_registered(fake_pool, monkeypatch, caplog):
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
    monkeypatch.setattr("app.worker.get_storage_client", lambda: mock_storage_client)

    with caplog.at_level(logging.INFO):
        await process_job(fake_pool, str(import_id))

    mock_mark_failed.assert_called_once_with(
        fake_pool, import_id=import_id, error_detail="No parser available yet for M-Pesa"
    )
    mock_storage_client.get_object.assert_not_called()
    assert any("No parser registered" in r.message for r in caplog.records)


async def test_process_job_fetches_content_and_calls_a_registered_parser(fake_pool, monkeypatch):
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

    mock_parser = MagicMock(return_value=[])
    monkeypatch.setattr("app.worker.PARSERS", {"Test Provider": mock_parser})

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
    mock_mark_failed.assert_not_called()


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
    assert any("Parser rejected import" in r.message for r in caplog.records)


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
