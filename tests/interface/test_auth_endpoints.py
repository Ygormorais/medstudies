"""Integration tests for /auth/* endpoints."""
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from medstudies.interface.api import app
    return TestClient(app, raise_server_exceptions=False)


def test_login_sends_magic_link(client):
    with patch("medstudies.interface.api.send_magic_link") as mock_ml:
        resp = client.post("/auth/login", json={"email": "doc@hospital.com"})
    assert resp.status_code == 200
    mock_ml.assert_called_once_with("doc@hospital.com")


def test_login_missing_email_returns_422(client):
    resp = client.post("/auth/login", json={})
    assert resp.status_code == 422


def test_callback_sets_cookies_on_success(client):
    fake_session = {
        "access_token": "acc123",
        "refresh_token": "ref456",
        "user": {"id": "uid-1"},
    }
    with patch("medstudies.interface.api.verify_otp", return_value=fake_session):
        resp = client.get(
            "/auth/callback?token_hash=abc&type=magiclink",
            follow_redirects=False,
        )
    assert resp.status_code in (302, 303)
    assert "ms_access" in resp.cookies
    assert "ms_refresh" in resp.cookies


def test_callback_expired_link_returns_400(client):
    with patch("medstudies.interface.api.verify_otp", side_effect=ValueError("link_expired")):
        resp = client.get("/auth/callback?token_hash=old&type=magiclink")
    assert resp.status_code == 400


def test_logout_clears_cookies(client):
    with patch("medstudies.interface.api.logout_server_side"):
        resp = client.post("/auth/logout", cookies={"ms_access": "sometoken"})
    assert resp.status_code == 200
    # Cookie cleared — max_age=0 or value empty
    set_cookie_headers = resp.headers.get_list("set-cookie") if hasattr(resp.headers, "get_list") else [resp.headers.get("set-cookie", "")]
    cookie_str = " ".join(set_cookie_headers)
    assert "ms_access" in cookie_str
