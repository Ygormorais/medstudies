"""Verify Supabase RS256 JWTs via JWKS. Caches keys for ~1 hour."""
import time
import threading
import httpx
from jose import jwt, JWTError, ExpiredSignatureError
from medstudies.auth.config import SUPABASE_URL

_JWKS_URL = f"{SUPABASE_URL}/auth/v1/jwks"
_CACHE_TTL = 3600  # seconds


class TokenExpiredError(Exception):
    pass


class InvalidTokenError(Exception):
    pass


_jwks_cache: dict | None = None
_jwks_cache_ts: float = 0.0
_jwks_lock = threading.Lock()


def _fetch_jwks() -> dict:
    resp = httpx.get(_JWKS_URL, timeout=5)
    resp.raise_for_status()
    return resp.json()


def _get_jwks() -> dict:
    global _jwks_cache, _jwks_cache_ts
    with _jwks_lock:
        if _jwks_cache is None or time.time() - _jwks_cache_ts > _CACHE_TTL:
            _jwks_cache = _fetch_jwks()
            _jwks_cache_ts = time.time()
        return _jwks_cache


def verify_token(token: str) -> str:
    """Verify a Supabase JWT and return the user_id (sub claim).

    Raises TokenExpiredError if expired, InvalidTokenError for any other failure.
    """
    jwks = _get_jwks()
    try:
        payload = jwt.decode(
            token,
            jwks,
            algorithms=["RS256"],
            audience="authenticated",
            options={"verify_exp": True},
        )
        return payload["sub"]
    except ExpiredSignatureError:
        raise TokenExpiredError("JWT expired")
    except JWTError as exc:
        raise InvalidTokenError(str(exc)) from exc
