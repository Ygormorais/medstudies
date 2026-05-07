"""FastAPI HTTP middleware that validates Supabase JWTs for /api/* routes."""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

import medstudies.auth.config as _auth_config
from medstudies.auth.config import COOKIE_ACCESS
from medstudies.auth.jwt_verifier import InvalidTokenError, TokenExpiredError, verify_token

_PUBLIC_PREFIXES = ("/auth/", "/static/", "/sw.js", "/manifest.json", "/favicon.ico")


class JWTAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not _auth_config.AUTH_ENABLED:
            return await call_next(request)

        path = request.url.path
        if path == "/" or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
            return await call_next(request)

        if path.startswith("/api/"):
            token = _extract_token(request)
            if not token:
                return JSONResponse({"detail": "Não autenticado"}, status_code=401)
            try:
                request.state.user_id = verify_token(token)
            except TokenExpiredError:
                return JSONResponse({"detail": "Sessão expirada"}, status_code=401)
            except InvalidTokenError:
                return JSONResponse({"detail": "Token inválido"}, status_code=401)

        return await call_next(request)


def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth.removeprefix("Bearer ")
    return request.cookies.get(COOKIE_ACCESS)
