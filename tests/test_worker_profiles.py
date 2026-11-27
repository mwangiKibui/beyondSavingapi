"""ab-312: the worker reads with a stored profile first, falls back to the
bespoke parser; ab-315: a file neither can read goes to the mapping wizard
(needs_mapping) unless it can't be mapped at all."""

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
        "status": "pending",
        "user_id": uuid4(),
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
    m.check_failed = AsyncMock()
    monkeypatch.setattr("app.worker.record_check_failed", m.check_failed)
    m.handle = AsyncMock()
    monkeypatch.setattr("app.worker._handle_rows", m.handle)
    m.status = AsyncMock()
    monkeypatch.setattr("app.worker.set_import_status", m.status)
    m.ready = AsyncMock()
    monkeypatch.setattr("app.worker.notify_statement_ready", m.ready)
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


async def test_a_failing_check_is_recorded_on_the_profile(pool, job):
    """ab-317: the admin list shows a layout's latest check result."""
    job.content = BAD
    c = candidate()
    job.profiles.return_value = [c]
    await process_job(pool, str(job.import_id))
    job.check_failed.assert_awaited_once_with(pool, profile_id=c["id"])
    job.checked.assert_not_called()


async def test_recording_a_failed_check_failing_doesnt_stop_the_import(pool, job, caplog):
    job.content = BAD
    job.profiles.return_value = [candidate()]
    job.check_failed.side_effect = RuntimeError("db hiccup")
    await process_job(pool, str(job.import_id))
    job.status.assert_awaited_once_with(pool, import_id=job.import_id, status="needs_mapping")
    assert "failed check" in caplog.text


async def test_a_failing_check_with_no_parser_goes_to_the_mapping_wizard(pool, job):
    job.content = BAD
    job.profiles.return_value = [candidate()]
    await process_job(pool, str(job.import_id))
    job.status.assert_awaited_once_with(pool, import_id=job.import_id, status="needs_mapping")
    job.failed.assert_not_called()
    job.insert.assert_not_called()


async def test_an_unknown_layout_is_fingerprinted_then_needs_mapping(pool, job):
    await process_job(pool, str(job.import_id))
    job.fingerprint.assert_awaited_once()
    job.status.assert_awaited_once_with(pool, import_id=job.import_id, status="needs_mapping")
    job.failed.assert_not_called()


async def test_only_another_institutions_profile_fits_and_there_is_no_parser(pool, job):
    job.profiles.return_value = [candidate(institution=uuid4(), name="Other Bank")]
    await process_job(pool, str(job.import_id))
    detail = job.failed.call_args.kwargs["error_detail"]
    assert detail == "This looks like a Other Bank statement. Check you uploaded it to the right account."
    job.status.assert_not_called()


async def test_a_file_without_text_fails_clearly(pool, job):
    job.content = b"%PDF-not really"
    await process_job(pool, str(job.import_id))
    assert "couldn't find any text" in job.failed.call_args.kwargs["error_detail"]
    job.status.assert_not_called()


async def test_an_account_without_an_institution_cant_be_mapped(pool, job):
    job.record["institution_id"] = None
    await process_job(pool, str(job.import_id))
    assert job.failed.call_args.kwargs["error_detail"] == "We can't read this file's layout yet"
    job.status.assert_not_called()


async def test_a_waiting_upload_read_at_last_tells_its_uploader(pool, job):
    job.record["status"] = "awaiting_support"
    job.profiles.return_value = [candidate()]
    await process_job(pool, str(job.import_id))
    assert job.parsed.call_args.kwargs["row_count"] == 6
    job.ready.assert_awaited_once_with(
        pool, user_id=job.record["user_id"], import_id=job.import_id, provider="Sample Bank"
    )


async def test_a_waiting_upload_that_still_doesnt_fit_keeps_waiting(pool, job):
    job.record["status"] = "awaiting_support"
    await process_job(pool, str(job.import_id))
    job.status.assert_not_called()
    job.failed.assert_not_called()
    job.ready.assert_not_called()


async def test_a_new_upload_read_by_a_profile_tells_nobody(pool, job):
    job.profiles.return_value = [candidate()]
    await process_job(pool, str(job.import_id))
    job.ready.assert_not_called()


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


# --- ab-319: spreadsheets, and the success log -----------------------------------


def _sheet_candidate():
    from app.parsers.profiles.fingerprint import fingerprint_file
    from tests.statement_sheets import HEADER, csv_bytes, rows

    content = csv_bytes([HEADER, *rows(4)])
    c = candidate()
    c["fingerprint"] = fingerprint_file(content, "x.csv").to_text()
    c["profile"] = {**PROFILE, "file_type": "csv", "table": {**PROFILE["table"], "column_bounds": "cells", "end_markers": []}}
    return content, c


async def test_a_csv_is_read_by_a_csv_profile_and_never_reaches_the_pdf_parser(pool, job, caplog):
    content, c = _sheet_candidate()
    job.content, job.record["file_name"] = content, "export.csv"
    job.profiles.return_value = [c]
    parser = MagicMock(return_value=[])
    job.parsers["Sample Bank"] = parser
    with caplog.at_level(logging.INFO):
        await process_job(pool, str(job.import_id))
    parser.assert_not_called()
    assert job.parsed.call_args.kwargs["profile_id"] == c["id"]
    assert len(job.insert.call_args.kwargs["transactions"]) == 4
    assert any("read by profile" in r.message for r in caplog.records)


async def test_a_csv_nothing_can_read_needs_mapping_without_trying_the_pdf_parser(pool, job):
    from tests.statement_sheets import HEADER, csv_bytes, rows

    job.content, job.record["file_name"] = csv_bytes([HEADER, *rows(2)]), "export.csv"
    parser = MagicMock(return_value=[])
    job.parsers["Sample Bank"] = parser
    await process_job(pool, str(job.import_id))
    parser.assert_not_called()
    job.status.assert_awaited_once_with(pool, import_id=job.import_id, status="needs_mapping")
    job.failed.assert_not_called()


async def test_a_successful_profile_read_is_logged(pool, job, caplog):
    job.profiles.return_value = [candidate()]
    with caplog.at_level(logging.INFO):
        await process_job(pool, str(job.import_id))
    assert any("read by profile" in r.message and "6 rows" in r.message for r in caplog.records)


# --- a layout the uploader just saved --------------------------------------------


@pytest.fixture
def chosen(job, monkeypatch):
    """The upload waits to be read with the layout its uploader saved, whose
    fingerprint no longer matches the file (older code took it)."""
    c = {**candidate(), "fingerprint": '{"v":0}'}
    job.record["profile_id"] = c["id"]
    job.profiles.return_value = [c]
    job.get_profile = AsyncMock(return_value=c)
    monkeypatch.setattr("app.worker.get_profile", job.get_profile)
    job.back = AsyncMock()
    monkeypatch.setattr("app.worker.return_to_mapping", job.back)
    job.propose = AsyncMock()
    monkeypatch.setattr("app.worker.propose_layout", job.propose)
    return c


async def test_a_saved_layout_reads_its_upload_even_when_fingerprints_differ(pool, job, chosen):
    await process_job(pool, str(job.import_id))

    job.get_profile.assert_awaited_once_with(pool, profile_id=chosen["id"])
    assert len(job.insert.call_args.kwargs["transactions"]) == 6
    assert job.parsed.call_args.kwargs["profile_id"] == chosen["id"]
    job.checked.assert_awaited_once_with(pool, profile_id=chosen["id"])
    job.back.assert_not_called()
    job.status.assert_not_called()


async def test_a_saved_layout_that_fails_goes_back_to_columns_saying_why(pool, job, chosen):
    job.content = BAD
    job.parsers["Sample Bank"] = MagicMock(return_value=[])
    await process_job(pool, str(job.import_id))

    detail = job.back.call_args.kwargs["error_detail"]
    assert job.back.call_args.kwargs["import_id"] == job.import_id
    assert detail.startswith("We couldn't read the whole statement with these columns: ")
    assert "balance" in detail.lower()
    job.parsers["Sample Bank"].assert_not_called()
    job.propose.assert_not_called()
    job.insert.assert_not_called()


async def test_only_a_pending_upload_uses_its_saved_layout(pool, job, chosen):
    job.record["status"] = "awaiting_support"
    job.profiles.return_value = []
    await process_job(pool, str(job.import_id))
    job.get_profile.assert_not_called()


async def test_a_saved_layout_that_cant_be_loaded_falls_back_to_matching(pool, job, chosen):
    job.get_profile.side_effect = RuntimeError("db down")
    job.profiles.return_value = [candidate()]
    await process_job(pool, str(job.import_id))
    assert len(job.insert.call_args.kwargs["transactions"]) == 6
    job.back.assert_not_called()
