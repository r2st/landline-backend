"""API key authentication middleware.

When settings.api_key is non-empty, every request must carry a matching
X-API-Key header — except for a small set of public paths (health checks,
docs, OAuth callbacks, webhooks).
"""

import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings

logger = logging.getLogger(__name__)

# Paths that never require an API key.
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
    "/api/v1/gmail/oauth/callback",
    "/api/v1/outreach/events",
    "/api/v1/track/",  # tracking pixels and click redirects must be public
)


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Reject requests with a missing or invalid X-API-Key header."""

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

        # Validate the API key header.
        provided_key = request.headers.get("X-API-Key", "")
        if provided_key != settings.api_key:
            logger.warning("Rejected request to %s — invalid or missing API key", path)
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing API key"},
            )

        return await call_next(request)
