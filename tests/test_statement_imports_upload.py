import io
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pypdf
import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app
from app.services.statement_files import IncorrectStatementPassword

ACCOUNT_ID = uuid4()
FAKE_ACCOUNT = {"id": str(ACCOUNT_ID), "nickname": "Equity Salary", "statements_uploadable": True}
PDF_CONTENT = b"%PDF-1.4 fake content"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fake_pool():
    """Bypasses the "database unavailable" check; tests using this always
    mock the repository calls too, so the fake pool object is never touched."""
    app.dependency_overrides[get_pool] = lambda: object()
    yield
    app.dependency_overrides.pop(get_pool, None)


@pytest.fixture
def fake_user():
    user_id = uuid4()
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    yield user_id
    app.dependency_overrides.pop(get_current_user_id, None)


def _upload(client, *, account_id=ACCOUNT_ID, password=None, filename="statement.pdf", content=PDF_CONTENT):
    data = {"account_id": str(account_id)}
    if password is not None:
        data["password"] = password
    return client.post(
        "/statement-imports",
        data=data,
        files={"file": (filename, content, "application/pdf")},
    )


def test_upload_statement_requires_auth(client):
    response = _upload(client)
    assert response.status_code == 401


def test_upload_statement_returns_503_when_db_unreachable(client, fake_user):
    # No fake_pool override: in this test environment there's no real
    # Postgres, so app.state.db_pool is genuinely None.
    response = _upload(client)
    assert response.status_code == 503


@pytest.mark.parametrize("filename", ["statement.txt", "statement.doc", "statement.docx", "statement.xls"])
def test_upload_statement_rejects_unsupported_file_type(client, fake_user, fake_pool, filename):
    # ab-319: Word files were accepted but never read; old .xls isn't read.
    response = _upload(client, filename=filename, content=b"not a statement")
    assert response.status_code == 422
    assert response.json()["detail"] == (
        "This file type can't be read. Download your statement as a PDF, CSV or Excel (.xlsx) file."
    )


def test_upload_statement_rejects_a_file_that_isnt_what_its_name_says(client, fake_user, fake_pool):
    response = _upload(client, filename="statement.csv", content=PDF_CONTENT)
    assert response.status_code == 422
    assert response.json()["detail"] == "This file isn't a CSV file"


def _accept_upload(monkeypatch, file_name):
    import_id = uuid4()
    row = {
        "id": import_id, "account_id": ACCOUNT_ID, "file_name": file_name, "status": "pending",
        "storage_key": f"statements/{import_id}/{file_name}", "created_at": "2026-01-01T00:00:00+00:00",
    }
    monkeypatch.setattr("app.api.statement_imports.get_account", AsyncMock(return_value=FAKE_ACCOUNT))
    monkeypatch.setattr("app.api.statement_imports.list_sub_ledgers", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.api.statement_imports.create_statement_import", AsyncMock(return_value=row))
    monkeypatch.setattr("app.api.statement_imports.add_import_sub_ledgers", AsyncMock())
    storage = MagicMock()
    monkeypatch.setattr("app.api.statement_imports.get_storage_client", lambda: storage)
    redis = MagicMock()
    redis.lpush = AsyncMock()
    monkeypatch.setattr("app.api.statement_imports.get_redis", lambda: redis)
    return storage


def test_a_csv_statement_is_accepted_without_a_password_step(client, fake_user, fake_pool, monkeypatch):
    storage = _accept_upload(monkeypatch, "export.csv")
    content = b"Date,Details,Amount\n01/10/2026,Shop,5.00\n"
    response = _upload(client, filename="export.csv", content=content, password="ignored")
    assert response.status_code == 201
    assert response.json()["file_type"] == "csv"
    assert storage.put_object.call_args.kwargs["Body"] == content


def test_an_xlsx_statement_is_accepted(client, fake_user, fake_pool, monkeypatch):
    from tests.test_statement_file_types import xlsx_bytes

    _accept_upload(monkeypatch, "export.xlsx")
    response = _upload(client, filename="export.xlsx", content=xlsx_bytes([["Date", "Details", "Amount"]]))
    assert response.status_code == 201
    assert response.json()["file_type"] == "xlsx"


def test_upload_statement_rejects_oversized_file(client, fake_user, fake_pool):
    oversized = b"x" * (10 * 1024 * 1024 + 1)
    response = _upload(client, content=oversized)
    assert response.status_code == 422


def test_upload_statement_rejects_account_not_owned(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.statement_imports.get_account", AsyncMock(return_value=None))

    response = _upload(client)

    assert response.status_code == 404


def test_upload_statement_rejects_wrong_password_without_side_effects(
    client, fake_user, fake_pool, monkeypatch
):
    monkeypatch.setattr(
        "app.api.statement_imports.get_account", AsyncMock(return_value=FAKE_ACCOUNT)
    )
    monkeypatch.setattr("app.api.statement_imports.list_sub_ledgers", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        "app.api.statement_imports.check_password",
        MagicMock(side_effect=IncorrectStatementPassword()),
    )
    mock_create = AsyncMock()
    mock_storage_client = MagicMock()
    mock_redis = MagicMock()
    mock_redis.lpush = AsyncMock()
    monkeypatch.setattr("app.api.statement_imports.create_statement_import", mock_create)
    monkeypatch.setattr(
        "app.api.statement_imports.get_storage_client", lambda: mock_storage_client
    )
    monkeypatch.setattr("app.api.statement_imports.get_redis", lambda: mock_redis)

    response = _upload(client, password="wrongpass")

    assert response.status_code == 422
    assert response.json()["detail"] == "Incorrect statement password"
    mock_create.assert_not_called()
    mock_storage_client.put_object.assert_not_called()
    mock_redis.lpush.assert_not_called()


def test_upload_statement_success(client, fake_user, fake_pool, monkeypatch):
    import_id = uuid4()
    created_row = {
        "id": import_id,
        "account_id": ACCOUNT_ID,
        "file_name": "statement.pdf",
        "status": "pending",
        "period_start": None,
        "period_end": None,
        "row_count": None,
        "error_detail": None,
        "storage_key": f"statements/{import_id}/statement.pdf",
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    monkeypatch.setattr(
        "app.api.statement_imports.get_account", AsyncMock(return_value=FAKE_ACCOUNT)
    )
    monkeypatch.setattr("app.api.statement_imports.list_sub_ledgers", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.api.statement_imports.check_password", MagicMock(return_value=None))
    monkeypatch.setattr(
        "app.api.statement_imports.decrypt_if_needed", MagicMock(return_value=PDF_CONTENT)
    )
    mock_create = AsyncMock(return_value=created_row)
    monkeypatch.setattr("app.api.statement_imports.create_statement_import", mock_create)
    monkeypatch.setattr("app.api.statement_imports.add_import_sub_ledgers", AsyncMock())
    mock_storage_client = MagicMock()
    monkeypatch.setattr(
        "app.api.statement_imports.get_storage_client", lambda: mock_storage_client
    )
    mock_redis = MagicMock()
    mock_redis.lpush = AsyncMock()
    monkeypatch.setattr("app.api.statement_imports.get_redis", lambda: mock_redis)

    response = _upload(client)

    assert response.status_code == 201
    body = response.json()
    assert body["file_name"] == "statement.pdf"
    assert body["file_type"] == "pdf"
    assert body["status"] == "pending"

    mock_storage_client.put_object.assert_called_once()
    put_kwargs = mock_storage_client.put_object.call_args.kwargs
    assert put_kwargs["Bucket"] == "statements"
    assert put_kwargs["Body"] == PDF_CONTENT
    assert put_kwargs["Key"].startswith("statements/")
    assert put_kwargs["Key"].endswith("/statement.pdf")

    _, create_kwargs = mock_create.call_args
    assert create_kwargs["account_id"] == ACCOUNT_ID
    assert create_kwargs["file_name"] == "statement.pdf"
    assert create_kwargs["storage_key"] == put_kwargs["Key"]

    mock_redis.lpush.assert_called_once()
    lpush_args = mock_redis.lpush.call_args.args
    assert lpush_args[0] == "parse_jobs"
    assert lpush_args[1] == str(create_kwargs["import_id"])


def test_upload_statement_rejects_sub_ledger_ids_for_an_account_with_none(
    client, fake_user, fake_pool, monkeypatch
):
    monkeypatch.setattr(
        "app.api.statement_imports.get_account", AsyncMock(return_value=FAKE_ACCOUNT)
    )
    monkeypatch.setattr("app.api.statement_imports.list_sub_ledgers", AsyncMock(return_value=[]))

    response = client.post(
        "/statement-imports",
        data={"account_id": str(ACCOUNT_ID), "sub_ledger_ids": str(uuid4())},
        files={"file": ("statement.pdf", PDF_CONTENT, "application/pdf")},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "This account has no sub-ledgers"


def test_upload_statement_requires_at_least_one_selected_sub_ledger(
    client, fake_user, fake_pool, monkeypatch
):
    monkeypatch.setattr(
        "app.api.statement_imports.get_account", AsyncMock(return_value=FAKE_ACCOUNT)
    )
    monkeypatch.setattr(
        "app.api.statement_imports.list_sub_ledgers",
        AsyncMock(return_value=[{"id": uuid4(), "name": "Ordinary Deposit"}]),
    )

    response = _upload(client)

    assert response.status_code == 422
    assert response.json()["detail"] == "Select at least one sub-ledger to import from this statement"


def test_upload_statement_rejects_a_sub_ledger_id_not_on_the_account(
    client, fake_user, fake_pool, monkeypatch
):
    monkeypatch.setattr(
        "app.api.statement_imports.get_account", AsyncMock(return_value=FAKE_ACCOUNT)
    )
    monkeypatch.setattr(
        "app.api.statement_imports.list_sub_ledgers",
        AsyncMock(return_value=[{"id": uuid4(), "name": "Ordinary Deposit"}]),
    )

    response = client.post(
        "/statement-imports",
        data={"account_id": str(ACCOUNT_ID), "sub_ledger_ids": str(uuid4())},
        files={"file": ("statement.pdf", PDF_CONTENT, "application/pdf")},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "One or more selected sub-ledgers don't belong to this account"


def test_upload_statement_persists_selected_sub_ledgers(client, fake_user, fake_pool, monkeypatch):
    sub_ledger_id = uuid4()
    import_id = uuid4()
    monkeypatch.setattr(
        "app.api.statement_imports.get_account", AsyncMock(return_value=FAKE_ACCOUNT)
    )
    monkeypatch.setattr(
        "app.api.statement_imports.list_sub_ledgers",
        AsyncMock(return_value=[{"id": sub_ledger_id, "name": "Instant Loan"}]),
    )
    monkeypatch.setattr("app.api.statement_imports.check_password", MagicMock(return_value=None))
    monkeypatch.setattr(
        "app.api.statement_imports.decrypt_if_needed", MagicMock(return_value=PDF_CONTENT)
    )
    monkeypatch.setattr(
        "app.api.statement_imports.create_statement_import",
        AsyncMock(
            return_value={
                "id": import_id,
                "account_id": ACCOUNT_ID,
                "file_name": "statement.pdf",
                "status": "pending",
                "period_start": None,
                "period_end": None,
                "row_count": None,
                "error_detail": None,
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ),
    )
    mock_add_import_sub_ledgers = AsyncMock()
    monkeypatch.setattr("app.api.statement_imports.add_import_sub_ledgers", mock_add_import_sub_ledgers)
    monkeypatch.setattr("app.api.statement_imports.get_storage_client", lambda: MagicMock())
    mock_redis = MagicMock()
    mock_redis.lpush = AsyncMock()
    monkeypatch.setattr("app.api.statement_imports.get_redis", lambda: mock_redis)

    response = client.post(
        "/statement-imports",
        data={"account_id": str(ACCOUNT_ID), "sub_ledger_ids": str(sub_ledger_id)},
        files={"file": ("statement.pdf", PDF_CONTENT, "application/pdf")},
    )

    assert response.status_code == 201
    _, kwargs = mock_add_import_sub_ledgers.call_args
    assert kwargs["sub_ledger_ids"] == [sub_ledger_id]


def test_upload_statement_stores_the_decrypted_content_not_the_original(client, fake_user, fake_pool, monkeypatch):
    # An end-to-end check against the real fixture (rather than a mock)
    # that a password-protected upload's stored bytes are genuinely
    # decrypted - the password itself is never persisted, so nothing
    # downstream could ever unlock the original encrypted bytes again.
    fixture_path = (
        Path(__file__).parent / "fixtures" / "statements" / "mpesa" / "statement_sample.pdf"
    )
    encrypted_content = fixture_path.read_bytes()

    monkeypatch.setattr(
        "app.api.statement_imports.get_account", AsyncMock(return_value=FAKE_ACCOUNT)
    )
    monkeypatch.setattr("app.api.statement_imports.list_sub_ledgers", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        "app.api.statement_imports.create_statement_import",
        AsyncMock(return_value={"id": uuid4(), "account_id": ACCOUNT_ID, "file_name": "statement_sample.pdf",
                                 "status": "pending", "period_start": None, "period_end": None,
                                 "row_count": None, "error_detail": None, "created_at": "2026-01-01T00:00:00+00:00"}),
    )
    monkeypatch.setattr("app.api.statement_imports.add_import_sub_ledgers", AsyncMock())
    mock_storage_client = MagicMock()
    monkeypatch.setattr("app.api.statement_imports.get_storage_client", lambda: mock_storage_client)
    mock_redis = MagicMock()
    mock_redis.lpush = AsyncMock()
    monkeypatch.setattr("app.api.statement_imports.get_redis", lambda: mock_redis)

    response = _upload(
        client, password="0000000", filename="statement_sample.pdf", content=encrypted_content
    )

    assert response.status_code == 201
    stored_content = mock_storage_client.put_object.call_args.kwargs["Body"]
    assert stored_content != encrypted_content
    reader = pypdf.PdfReader(io.BytesIO(stored_content))
    assert not reader.is_encrypted


def test_upload_statement_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr(
        "app.api.statement_imports.get_account",
        AsyncMock(side_effect=RuntimeError("connection reset by peer, secret=xyz")),
    )

    with caplog.at_level(logging.ERROR):
        response = _upload(client)

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    assert error_records[0].exc_info is not None


def test_upload_statement_refused_for_a_provider_without_uploads(client, fake_user, fake_pool, monkeypatch):
    # ab-294: e.g. a money market fund - the app has no reader for it yet.
    monkeypatch.setattr(
        "app.api.statement_imports.get_account",
        AsyncMock(return_value={**FAKE_ACCOUNT, "statements_uploadable": False}),
    )
    mock_create = AsyncMock()
    monkeypatch.setattr("app.api.statement_imports.create_statement_import", mock_create)

    response = _upload(client)

    assert response.status_code == 422
    assert response.json()["detail"] == "Statements can't be uploaded for this provider yet"
    mock_create.assert_not_called()



def test_upload_allowed_when_an_active_profile_recognises_the_layout(client, fake_user, fake_pool, monkeypatch):
    # ab-312: a stored profile reads the provider's statements even though
    # no bespoke parser marked it uploadable.
    monkeypatch.setattr(
        "app.api.statement_imports.get_account",
        AsyncMock(return_value={**FAKE_ACCOUNT, "statements_uploadable": False, "recognised_layout": True}),
    )
    mock_create = AsyncMock(side_effect=RuntimeError("stop after the gate"))
    monkeypatch.setattr("app.api.statement_imports.create_statement_import", mock_create)

    response = _upload(client)

    assert response.json().get("detail") != "Statements can't be uploaded for this provider yet"


# --- ab-309: statement_imports_per_day ---------------------------------------


def test_upload_at_the_daily_limit_is_429_before_any_work(client, fake_user, fake_pool, monkeypatch, limits):
    limits(5, used=5)
    mock_get_account = AsyncMock(return_value=FAKE_ACCOUNT)
    mock_create = AsyncMock()
    monkeypatch.setattr("app.api.statement_imports.get_account", mock_get_account)
    monkeypatch.setattr("app.api.statement_imports.create_statement_import", mock_create)

    response = _upload(client)

    assert response.status_code == 429
    body = response.json()["detail"]
    assert {k: body[k] for k in ("code", "limit_key", "used", "limit")} == {
        "code": "limit_reached", "limit_key": "statement_imports_per_day", "used": 5, "limit": 5,
    }
    assert body["resets_at"].endswith("T00:00:00+03:00")
    mock_get_account.assert_not_awaited()
    mock_create.assert_not_awaited()


def test_upload_below_the_daily_limit_goes_ahead(client, fake_user, fake_pool, monkeypatch, limits):
    limits(5, used=4)
    monkeypatch.setattr("app.api.statement_imports.get_account", AsyncMock(return_value=None))

    assert _upload(client).status_code == 404  # past the limit check, on to the account


def test_upload_limit_check_failing_is_a_generic_500(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.services.limits.get_plan_limit", AsyncMock(side_effect=RuntimeError("db down")))

    response = _upload(client)

    assert response.status_code == 500
    assert "db down" not in response.text
