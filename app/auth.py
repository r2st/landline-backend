"""API key + JWT authentication middleware.

When settings.api_key is non-empty, every request must carry either:
  1. A matching ``X-API-Key`` header, OR
  2. A valid JWT ``Authorization: Bearer <token>``

…except for a small set of public paths (health checks, docs, OAuth
callbacks, webhooks, and the auth endpoints themselves).
"""

import hmac
import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from jose import JWTError
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings

logger = logging.getLogger(__name__)

# Paths that never require authentication.
PUBLIC_PATHS = frozenset({
    "/health",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/",
    "/api/v1/health",
})

# Path prefixes that are also public.
PUBLIC_PREFIXES = (
    "/api/v1/auth/",          # login, refresh — must be reachable without auth
    "/api/v1/gmail/oauth/callback",
    "/api/v1/outreach/events",
    "/api/v1/track/",         # tracking pixels and click redirects must be public
)


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Reject requests with a missing or invalid X-API-Key / Bearer token."""

    async def dispatch(self, request: Request, call_next):
        settings = get_settings()

        # If no API key is configured, auth is disabled (backward compat).
        if not settings.api_key:
            return await call_next(request)

        # Let CORS preflight (OPTIONS) requests through without auth.
        if request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path

        # Allow public paths through without auth.
        if path in PUBLIC_PATHS:
            return await call_next(request)

        for prefix in PUBLIC_PREFIXES:
            if path.startswith(prefix):
                return await call_next(request)

        # Try API key first.
        provided_key = request.headers.get("X-API-Key", "")
        if provided_key and hmac.compare_digest(provided_key, settings.api_key):
            return await call_next(request)

        # Try JWT Bearer token.
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            try:
                from app.security import decode_token
                payload = decode_token(token)
                if payload.get("type") == "access":
                    return await call_next(request)
            except (JWTError, Exception):
                pass  # fall through to 401

        logger.warning("Rejected request to %s — invalid or missing credentials", path)
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or missing API key / Bearer token"},
        )


# Preferred name — this middleware accepts an API key OR a JWT Bearer token.
# ``ApiKeyMiddleware`` is kept as a backward-compatible alias.
AuthMiddleware = ApiKeyMiddleware
