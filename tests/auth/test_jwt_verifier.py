"""JWT verifier tests using a locally-generated RSA key pair (no Supabase)."""
import time
import pytest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from jose import jwt
from jose.utils import long_to_base64


def _make_rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture(scope="module")
def rsa_keypair():
    return _make_rsa_keypair()


@pytest.fixture(scope="module")
def private_pem(rsa_keypair):
    priv, _ = rsa_keypair
    return priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )


@pytest.fixture(scope="module")
def jwks_json(rsa_keypair):
    """Build a minimal JWKS response from the public key."""
    _, pub = rsa_keypair
    pub_numbers = pub.public_numbers()
    n_b64 = long_to_base64(pub_numbers.n).decode()
    e_b64 = long_to_base64(pub_numbers.e).decode()
    return {"keys": [{"kty": "RSA", "alg": "RS256", "use": "sig", "kid": "test-key-1",
                      "n": n_b64, "e": e_b64}]}


def _make_token(private_pem: bytes, sub: str, exp_offset: int = 3600) -> str:
    return jwt.encode(
        {"sub": sub, "exp": int(time.time()) + exp_offset, "aud": "authenticated"},
        private_pem,
        algorithm="RS256",
        headers={"kid": "test-key-1"},
    )


def test_verify_valid_token(jwks_json, private_pem):
    from medstudies.auth.jwt_verifier import verify_token
    token = _make_token(private_pem, sub="user-uuid-123")
    with patch("medstudies.auth.jwt_verifier._fetch_jwks", return_value=jwks_json):
        user_id = verify_token(token)
    assert user_id == "user-uuid-123"


def test_verify_expired_token_raises(jwks_json, private_pem):
    from medstudies.auth.jwt_verifier import verify_token, TokenExpiredError
    token = _make_token(private_pem, sub="user-uuid-123", exp_offset=-10)
    with patch("medstudies.auth.jwt_verifier._fetch_jwks", return_value=jwks_json):
        with pytest.raises(TokenExpiredError):
            verify_token(token)


def test_verify_invalid_signature_raises(jwks_json):
    from medstudies.auth.jwt_verifier import verify_token, InvalidTokenError
    bad_token = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.invalidsig"
    with patch("medstudies.auth.jwt_verifier._fetch_jwks", return_value=jwks_json):
        with pytest.raises(InvalidTokenError):
            verify_token(bad_token)


def test_jwks_cached(jwks_json, private_pem):
    """_fetch_jwks should be called only once across multiple verifications."""
    import medstudies.auth.jwt_verifier as jv
    jv._jwks_cache = None  # reset cache
    jv._jwks_cache_ts = 0.0
    token = _make_token(private_pem, sub="user-uuid-456")
    with patch("medstudies.auth.jwt_verifier._fetch_jwks", return_value=jwks_json) as m:
        jv.verify_token(token)
        jv.verify_token(token)
        assert m.call_count == 1  # second call uses cache
