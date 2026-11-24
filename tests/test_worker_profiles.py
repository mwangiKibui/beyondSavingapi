"""ab-312: the worker reads with a stored profile first, falls back to the
bespoke parser, and only fails when neither can read the file."""

import logging
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.parsers.base import StatementParseError
from app.parsers.profiles.fingerprint import fingerprint_pdf
from app.worker import process_job
from tests.statement_pdfs import Layout, build, sample_rows

INSTITUTION = uuid4()
PROFILE = {
    "date_format": "%d/%m/%Y",
    "table": {
        "columns": [
            {"role": "date", "header": "Date"},
            {"role": "description", "header": "Details"},
            {"role": "money_in", "header": "Money In"},
            {"role": "money_out", "header": "Money Out"},
            {"role": "balance", "header": "Balance"},
        ],
        "column_bounds": "header_gaps",
        "end_markers": ["End of statement"],
    },
}
GOOD = build(sample_rows(6))
BAD = build([*sample_rows(3), ["05/10/2026", "Odd", "", "10.00", "5.00"]])


def candidate(institution=INSTITUTION, name="Sample Bank"):
    return {
        "id": uuid4(),
        "institution_id": institution,
        "institution_name": name,
        "fingerprint": fingerprint_pdf(GOOD).to_text(),
        "profile": PROFILE,
        "version": 1,
        "usage_count": 0,
        "last_check_passed_at": None,
    }


@pytest.fixture
def pool():
    return object()


@pytest.fixture
def job(monkeypatch):
    """Patches the worker's I/O; returns the mocks and a setter for the file."""
    import_id, account_id = uuid4(), uuid4()
    record = {
        "id": import_id,
        "account_id": account_id,
        "storage_key": "statements/x/s.pdf",
        "file_name": "s.pdf",
        "provider": "Sample Bank",
        "currency": "KES",
        "institution_id": INSTITUTION,
    }
    m = MagicMock()
    m.import_id, m.account_id, m.record = import_id, account_id, record
    m.content = GOOD
    monkeypatch.setattr("app.worker.get_import_for_processing", AsyncMock(return_value=record))
    m.profiles = AsyncMock(return_value=[])
    monkeypatch.setattr("app.worker.list_active_profiles", m.profiles)
    m.failed = AsyncMock()
    monkeypatch.setattr("app.worker.mark_import_failed", m.failed)
    m.parsed = AsyncMock()
    monkeypatch.setattr("app.worker.mark_import_parsed", m.parsed)
    m.insert = AsyncMock(side_effect=lambda *a, transactions, **k: len(transactions))
    monkeypatch.setattr("app.worker.insert_transactions", m.insert)
    m.fingerprint = AsyncMock()
    monkeypatch.setattr("app.worker.set_import_fingerprint", m.fingerprint)
    m.checked = AsyncMock()
    monkeypatch.setattr("app.worker.record_check_passed", m.checked)
    m.handle = AsyncMock()
    monkeypatch.setattr("app.worker._handle_rows", m.handle)
    m.parsers = {}
    monkeypatch.setattr("app.worker.PARSERS", m.parsers)
    storage = MagicMock()
    storage.get_object.side_effect = lambda **k: {"Body": MagicMock(read=MagicMock(return_value=m.content))}
    m.storage = storage
    monkeypatch.setattr("app.worker.get_storage_client", lambda: storage)
    return m


async def test_a_matching_profile_reads_the_file(pool, job):
    c = candidate()
    job.profiles.return_value = [c]
    await process_job(pool, str(job.import_id))

    rows = job.insert.call_args.kwargs["transactions"]
    assert len(rows) == 6
    assert job.parsed.call_args.kwargs["profile_id"] == c["id"]
    assert job.parsed.call_args.kwargs["row_count"] == 6
    job.checked.assert_awaited_once_with(pool, profile_id=c["id"])
    job.fingerprint.assert_awaited_once_with(pool, import_id=job.import_id, fingerprint=c["fingerprint"])
    job.handle.assert_awaited_once()
    job.failed.assert_not_called()


async def test_the_profile_wins_over_a_bespoke_parser(pool, job):
    job.profiles.return_value = [candidate()]
    parser = MagicMock(return_value=[])
    job.parsers["Sample Bank"] = parser
    await process_job(pool, str(job.import_id))
    parser.assert_not_called()


async def test_a_failing_check_falls_back_to_the_bespoke_parser(pool, job, caplog):
    job.content = BAD
    job.profiles.return_value = [candidate()]
    parser = MagicMock(return_value=[])
    job.parsers["Sample Bank"] = parser
    with caplog.at_level(logging.WARNING):
        await process_job(pool, str(job.import_id))
    parser.assert_called_once_with(BAD)
    assert "profile_id" not in job.parsed.call_args.kwargs
    job.checked.assert_not_called()
    job.fingerprint.assert_awaited_once()
    assert any("falling back" in r.message for r in caplog.records)


async def test_a_failing_check_with_no_parser_fails_with_the_check_message(pool, job):
    job.content = BAD
    job.profiles.return_value = [candidate()]
    await process_job(pool, str(job.import_id))
    detail = job.failed.call_args.kwargs["error_detail"]
    assert "balance" in detail.lower()
    job.insert.assert_not_called()


async def test_no_parser_and_no_profile_fails_before_downloading(pool, job):
    await process_job(pool, str(job.import_id))
    job.failed.assert_awaited_once_with(pool, import_id=job.import_id, error_detail="No parser available yet for Sample Bank")
    job.storage.get_object.assert_not_called()


async def test_only_another_institutions_profile_fits_and_there_is_no_parser(pool, job):
    job.profiles.return_value = [candidate(institution=uuid4(), name="Other Bank")]
    await process_job(pool, str(job.import_id))
    job.storage.get_object.assert_not_called()
    job.failed.assert_awaited_once()


async def test_a_wrong_institution_match_is_named_when_nothing_reads_it(pool, job):
    job.profiles.return_value = [candidate(), candidate(institution=uuid4(), name="Other Bank")]
    # Own profile exists but this file is the other bank's different layout.
    other_layout = Layout(headers=["Posted", "Narrative", "Credit", "Debit", "Running"])
    job.profiles.return_value[0]["fingerprint"] = fingerprint_pdf(build(sample_rows(2), other_layout)).to_text()
    await process_job(pool, str(job.import_id))
    detail = job.failed.call_args.kwargs["error_detail"]
    assert detail == "This looks like a Other Bank statement. Check you uploaded it to the right account."


async def test_a_wrong_institution_match_only_warns_when_the_parser_reads_it(pool, job, caplog):
    job.profiles.return_value = [candidate(institution=uuid4(), name="Other Bank")]
    parser = MagicMock(return_value=[])
    job.parsers["Sample Bank"] = parser
    with caplog.at_level(logging.WARNING):
        await process_job(pool, str(job.import_id))
    parser.assert_called_once()
    job.failed.assert_not_called()
    assert any("Other Bank" in r.message for r in caplog.records)


async def test_profiles_failing_to_load_dont_stop_the_bespoke_parser(pool, job, caplog):
    job.profiles.side_effect = RuntimeError("db down")
    parser = MagicMock(return_value=[])
    job.parsers["Sample Bank"] = parser
    with caplog.at_level(logging.ERROR):
        await process_job(pool, str(job.import_id))
    parser.assert_called_once()
    job.failed.assert_not_called()


async def test_an_account_without_an_institution_skips_profiles(pool, job):
    job.record["institution_id"] = None
    parser = MagicMock(return_value=[])
    job.parsers["Sample Bank"] = parser
    await process_job(pool, str(job.import_id))
    job.profiles.assert_not_called()
    parser.assert_called_once()


async def test_a_bespoke_parser_error_still_fails_as_before(pool, job):
    job.parsers["Sample Bank"] = MagicMock(side_effect=StatementParseError("Running balance mismatch at transaction 2"))
    await process_job(pool, str(job.import_id))
    job.failed.assert_awaited_once_with(
        pool, import_id=job.import_id, error_detail="Running balance mismatch at transaction 2"
    )


async def test_storing_the_fingerprint_failing_doesnt_fail_the_import(pool, job):
    job.fingerprint.side_effect = RuntimeError("column missing")
    job.profiles.return_value = [candidate()]
    await process_job(pool, str(job.import_id))
    job.failed.assert_not_called()
    job.parsed.assert_awaited_once()
