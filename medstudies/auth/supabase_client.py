"""Thin httpx wrapper for Supabase Auth API calls."""
import httpx
from medstudies.auth.config import SUPABASE_URL, SUPABASE_ANON_KEY, MEDSTUDIES_BASE_URL

_CALLBACK_URL = f"{MEDSTUDIES_BASE_URL}/auth/callback"


def _headers() -> dict:
    return {"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"}


def send_magic_link(email: str) -> None:
    """Trigger Supabase to send a magic-link email."""
    resp = httpx.post(
        f"{SUPABASE_URL}/auth/v1/otp",
        json={"email": email, "create_user": True},
        params={"redirect_to": _CALLBACK_URL},
        headers=_headers(),
        timeout=10,
    )
    resp.raise_for_status()


def verify_otp(token_hash: str, otp_type: str = "magiclink") -> dict:
    """Exchange the token_hash from the callback URL for a session.

    Returns Supabase session dict with access_token, refresh_token, user.
    Raises ValueError("link_expired") if Supabase returns 422.
    """
    resp = httpx.post(
        f"{SUPABASE_URL}/auth/v1/verify",
        json={"token_hash": token_hash, "type": otp_type},
        headers=_headers(),
        timeout=10,
    )
    if resp.status_code == 422:
        raise ValueError("link_expired")
    resp.raise_for_status()
    return resp.json()


def refresh_session(refresh_token: str) -> dict:
    """Exchange a refresh token for a new access + refresh token pair.

    Raises ValueError("session_invalid") if token is revoked or invalid.
    """
    resp = httpx.post(
        f"{SUPABASE_URL}/auth/v1/token",
        params={"grant_type": "refresh_token"},
        json={"refresh_token": refresh_token},
        headers=_headers(),
        timeout=10,
    )
    if resp.status_code in (400, 401):
        raise ValueError("session_invalid")
    resp.raise_for_status()
    return resp.json()


def logout_server_side(access_token: str) -> None:
    """Revoke the session server-side (Supabase invalidates refresh token)."""
    httpx.post(
        f"{SUPABASE_URL}/auth/v1/logout",
        headers={**_headers(), "Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
