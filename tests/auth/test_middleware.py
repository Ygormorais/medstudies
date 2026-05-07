"""Tests for auth middleware — AUTH_ENABLED=true scenario."""
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from medstudies.persistence.models import Base
import medstudies.auth.jwt_verifier as _jv


def _reset_jwks_cache():
    _jv._jwks_cache = None
    _jv._jwks_cache_ts = 0.0


def _make_test_jwks_and_token():
    """Returns (jwks_dict, valid_jwt_token) using a fresh RSA keypair."""
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    from jose import jwt
    from jose.utils import long_to_base64
    import time

    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    pub_numbers = priv.public_key().public_numbers()
    jwks = {"keys": [{"kty": "RSA", "alg": "RS256", "use": "sig", "kid": "k1",
                      "n": long_to_base64(pub_numbers.n).decode(),
                      "e": long_to_base64(pub_numbers.e).decode()}]}
    token = jwt.encode(
        {"sub": "user-1", "exp": int(time.time()) + 3600, "aud": "authenticated"},
        pem, algorithm="RS256", headers={"kid": "k1"},
    )
    return jwks, token


@pytest.fixture
def auth_client():
    # StaticPool + check_same_thread=False: all threads share the same in-memory SQLite connection
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = Session(engine)
    with patch("medstudies.auth.config.AUTH_ENABLED", True), \
         patch("medstudies.interface.api.get_session", return_value=db):
        from medstudies.interface.api import app
        client = TestClient(app, raise_server_exceptions=False)
        yield client
    db.close()


def test_api_without_token_returns_401(auth_client):
    resp = auth_client.get("/api/topics")
    assert resp.status_code == 401


def test_api_with_invalid_token_returns_401(auth_client):
    jwks, _token = _make_test_jwks_and_token()
    _reset_jwks_cache()
    with patch("medstudies.auth.jwt_verifier._fetch_jwks", return_value=jwks):
        resp = auth_client.get("/api/topics", headers={"Authorization": "Bearer bad.token"})
    assert resp.status_code == 401


def test_api_with_valid_bearer_token_passes(auth_client):
    jwks, token = _make_test_jwks_and_token()
    _reset_jwks_cache()
    with patch("medstudies.auth.jwt_verifier._fetch_jwks", return_value=jwks):
        resp = auth_client.get("/api/topics", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


def test_api_with_valid_cookie_passes(auth_client):
    jwks, token = _make_test_jwks_and_token()
    _reset_jwks_cache()
    with patch("medstudies.auth.jwt_verifier._fetch_jwks", return_value=jwks):
        auth_client.cookies.set("ms_access", token)
        resp = auth_client.get("/api/topics")
        auth_client.cookies.clear()
    assert resp.status_code == 200


def test_dashboard_is_public(auth_client):
    resp = auth_client.get("/")
    assert resp.status_code == 200


def test_auth_path_is_public(auth_client):
    # /auth/* should not require token (even if endpoint doesn't exist yet)
    resp = auth_client.get("/auth/callback?token_hash=x&type=magiclink")
    assert resp.status_code in (200, 302, 303, 404)  # not 401 — auth bypass working
