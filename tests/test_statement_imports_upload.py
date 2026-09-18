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
FAKE_ACCOUNT = {"id": str(ACCOUNT_ID), "nickname": "Equity Salary"}
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


def test_upload_statement_rejects_unsupported_file_type(client, fake_user, fake_pool):
    response = _upload(client, filename="statement.txt", content=b"not a statement")
    assert response.status_code == 422


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
    monkeypatch.setattr("app.api.statement_imports.check_password", MagicMock(return_value=None))
    monkeypatch.setattr(
        "app.api.statement_imports.decrypt_if_needed", MagicMock(return_value=PDF_CONTENT)
    )
    mock_create = AsyncMock(return_value=created_row)
    monkeypatch.setattr("app.api.statement_imports.create_statement_import", mock_create)
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
    monkeypatch.setattr(
        "app.api.statement_imports.create_statement_import",
        AsyncMock(return_value={"id": uuid4(), "account_id": ACCOUNT_ID, "file_name": "statement_sample.pdf",
                                 "status": "pending", "period_start": None, "period_end": None,
                                 "row_count": None, "error_detail": None, "created_at": "2026-01-01T00:00:00+00:00"}),
    )
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
