"""API key + JWT authentication middleware.

The API fails **closed**: every request must carry either

  1. A matching ``X-API-Key`` header (when an API key is configured), OR
  2. A structurally-valid JWT ``Authorization: Bearer <token>`` (signature +
     ``access`` type + non-expired),

…except for a small set of public paths (health checks, docs, OAuth
callbacks, webhooks, and the auth endpoints themselves).

This middleware is always installed (see ``app.main``). Unlike the previous
behaviour, it does **not** disable itself when no API key is configured —
leaving the API unauthenticated in that case was a fail-open bug.

**User existence / is_active** is enforced one layer in, by the
``get_current_user`` dependency (applied to every data router) and by
``get_current_user_optional`` (used by scoped routers, which now rejects a
present-but-invalid token rather than silently falling back to unscoped). The
middleware deliberately stays DB-free so it adds no query to every request; the
two layers together guarantee a token for a deleted/deactivated user is
rejected on any authenticated route.
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
    """Reject requests with missing/invalid credentials — fail closed."""

    async def dispatch(self, request: Request, call_next):
        settings = get_settings()

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

        # Try API key first (only when one is configured).
        provided_key = request.headers.get("X-API-Key", "")
        if (
            settings.api_key
            and provided_key
            and hmac.compare_digest(provided_key, settings.api_key)
        ):
            return await call_next(request)

        # Try JWT Bearer token — must be a valid, non-expired access token.
        # User existence / is_active is enforced by the route dependencies.
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            try:
                from app.security import decode_token
                payload = decode_token(token)
                if payload.get("type") == "access":
                    return await call_next(request)
            except JWTError:
                pass  # invalid/expired token → fall through to 401
            except Exception:  # noqa: BLE001 — never leak an auth-path error as 200
                logger.exception("Auth middleware error validating token")

        logger.warning("Rejected request to %s — invalid or missing credentials", path)
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or missing API key / Bearer token"},
        )


# Preferred name — this middleware accepts an API key OR a JWT Bearer token.
# ``ApiKeyMiddleware`` is kept as a backward-compatible alias.
AuthMiddleware = ApiKeyMiddleware
