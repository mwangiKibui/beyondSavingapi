from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.security import AuthContext, get_auth_context, get_current_user_id

protected_app = FastAPI()


@protected_app.get("/protected")
async def protected_route(user_id=Depends(get_current_user_id)):
    return {"user_id": str(user_id)}


@protected_app.post("/protected")
async def protected_write_route(user_id=Depends(get_current_user_id)):
    return {"user_id": str(user_id)}


@protected_app.get("/context")
async def context_route(ctx: AuthContext = Depends(get_auth_context)):
    return {
        "user_id": str(ctx.user_id),
        "impersonated_by": str(ctx.impersonated_by) if ctx.impersonated_by else None,
        "imp_session_id": str(ctx.imp_session_id) if ctx.imp_session_id else None,
        "read_only": ctx.read_only,
    }


client = TestClient(protected_app)


def _token(sub: str, exp: datetime | None = None, **extra_claims) -> str:
    settings = get_settings()
    payload = {
        "sub": sub,
        "exp": exp or datetime.now(timezone.utc) + timedelta(minutes=5),
        **extra_claims,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def test_rejects_missing_token():
    response = client.get("/protected")
    assert response.status_code == 401


def test_rejects_malformed_token():
    response = client.get("/protected", headers={"Authorization": "Bearer not-a-real-jwt"})
    assert response.status_code == 401


def test_rejects_expired_token():
    token = _token(str(uuid4()), exp=datetime.now(timezone.utc) - timedelta(minutes=1))
    response = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_rejects_wrong_signature():
    token = jwt.encode({"sub": str(uuid4())}, "wrong-secret", algorithm="HS256")
    response = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_rejects_token_without_sub_claim():
    settings = get_settings()
    token = jwt.encode(
        {"exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    response = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_accepts_valid_token_and_returns_user_id():
    user_id = uuid4()
    token = _token(str(user_id))
    response = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["user_id"] == str(user_id)


def test_token_without_impersonation_claims_behaves_normally():
    user_id = uuid4()
    token = _token(str(user_id))
    get_response = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    post_response = client.post("/protected", headers={"Authorization": f"Bearer {token}"})
    assert get_response.status_code == 200
    assert post_response.status_code == 200


def test_read_only_impersonation_token_allows_get():
    user_id = uuid4()
    token = _token(str(user_id), read_only=True, impersonated_by=str(uuid4()))
    response = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["user_id"] == str(user_id)


def test_read_only_impersonation_token_blocks_post():
    user_id = uuid4()
    token = _token(str(user_id), read_only=True, impersonated_by=str(uuid4()))
    response = client.post("/protected", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403


def test_full_access_impersonation_token_allows_post():
    user_id = uuid4()
    token = _token(str(user_id), read_only=False, impersonated_by=str(uuid4()))
    response = client.post("/protected", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200


def test_auth_context_round_trips_impersonation_claims():
    user_id, admin_id, session_id = uuid4(), uuid4(), uuid4()
    token = _token(
        str(user_id),
        impersonated_by=str(admin_id),
        imp_session_id=str(session_id),
        read_only=True,
    )
    response = client.get("/context", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json() == {
        "user_id": str(user_id),
        "impersonated_by": str(admin_id),
        "imp_session_id": str(session_id),
        "read_only": True,
    }


def test_auth_context_defaults_absent_impersonation_claims():
    user_id = uuid4()
    token = _token(str(user_id))
    response = client.get("/context", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json() == {
        "user_id": str(user_id),
        "impersonated_by": None,
        "imp_session_id": None,
        "read_only": False,
    }
