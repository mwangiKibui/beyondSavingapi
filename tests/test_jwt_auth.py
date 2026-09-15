from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.security import get_current_user_id

protected_app = FastAPI()


@protected_app.get("/protected")
async def protected_route(user_id=Depends(get_current_user_id)):
    return {"user_id": str(user_id)}


client = TestClient(protected_app)


def _token(sub: str, exp: datetime | None = None) -> str:
    settings = get_settings()
    payload = {"sub": sub, "exp": exp or datetime.now(timezone.utc) + timedelta(minutes=5)}
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
