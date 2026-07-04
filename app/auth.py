"""Authentication: API key middleware + JWT dependencies + role guards.

Two layers work together:

1. ``AuthMiddleware`` — global gate. When ``settings.api_key`` is configured,
   every request (outside a small public allowlist) must carry EITHER a valid
   ``X-API-Key`` header (legacy, backward-compatible) OR a valid JWT access
   token (``Authorization: Bearer <token>``). This protects all existing routes
   without touching every router. When no API key is configured (e.g. the test
   suite), the gate is disabled — same behavior as before this change.

2. FastAPI dependencies — ``get_current_user`` / ``require_role(...)`` — used by
   the auth and admin routers to resolve the logged-in ``User`` from the bearer
   token and enforce role-based access. These run inside the request, after the
   middleware, and have DB access.
"""

import hmac
import logging
import uuid
from typing import Callable

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings
from app.database import get_db
from app.models.user import User, UserRole
from app.security import ACCESS_TOKEN_TYPE, decode_token

logger = logging.getLogger(__name__)

# Paths that never require auth.
PUBLIC_PATHS = frozenset({
    "/health",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/",
    "/api/v1/health",
    # Auth endpoints must be reachable without a token.
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
})

# Path prefixes that are also public.
PUBLIC_PREFIXES = (
    "/api/v1/gmail/oauth/callback",
    "/api/v1/outreach/events",
    "/api/v1/track/",  # tracking pixels and click redirects must be public
)

# Bearer scheme for OpenAPI docs + header extraction. auto_error=False so we can
# return our own 401 shape and let API-key auth fall through.
bearer_scheme = HTTPBearer(auto_error=False)


def _valid_jwt_access_token(token: str) -> bool:
    """True when ``token`` is a well-formed, unexpired access token."""
    try:
        claims = decode_token(token)
    except jwt.PyJWTError:
        return False
    return claims.get("type") == ACCESS_TOKEN_TYPE


class AuthMiddleware(BaseHTTPMiddleware):
    """Reject requests lacking a valid API key or JWT access token."""

    async def dispatch(self, request: Request, call_next):
        settings = get_settings()

        # If no API key is configured, auth is disabled (backward compat).
        if not settings.api_key:
            return await call_next(request)

        # Let CORS preflight (OPTIONS) requests through without auth.
        if request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path

        if path in PUBLIC_PATHS:
            return await call_next(request)
        for prefix in PUBLIC_PREFIXES:
            if path.startswith(prefix):
                return await call_next(request)

        # Accept a valid API key (legacy) ...
        provided_key = request.headers.get("X-API-Key", "")
        if provided_key and hmac.compare_digest(provided_key, settings.api_key):
            return await call_next(request)

        # ... or a valid JWT bearer access token.
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[len("Bearer "):].strip()
            if _valid_jwt_access_token(token):
                return await call_next(request)

        logger.warning("Rejected request to %s — no valid API key or bearer token", path)
        return JSONResponse(
            status_code=401,
            content={"detail": "Not authenticated"},
        )


# Backward-compatible alias (older imports referenced ApiKeyMiddleware).
ApiKeyMiddleware = AuthMiddleware


# ─── Dependencies ────────────────────────────────────────────────────────────

_CREDENTIALS_EXC = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Resolve the logged-in user from the JWT bearer token.

    Raises 401 when the token is missing/invalid/expired or the user no longer
    exists or has been deactivated.
    """
    if credentials is None or not credentials.credentials:
        raise _CREDENTIALS_EXC

    try:
        claims = decode_token(credentials.credentials)
    except jwt.PyJWTError:
        raise _CREDENTIALS_EXC

    if claims.get("type") != ACCESS_TOKEN_TYPE:
        raise _CREDENTIALS_EXC

    subject = claims.get("sub")
    if not subject:
        raise _CREDENTIALS_EXC
    try:
        user_id = uuid.UUID(str(subject))
    except (ValueError, TypeError):
        raise _CREDENTIALS_EXC

    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()

    if user is None or not user.is_active:
        raise _CREDENTIALS_EXC
    return user


def require_role(*roles: str) -> Callable:
    """Dependency factory: require the current user to hold one of ``roles``.

    Example::

        @router.get("/users", dependencies=[Depends(require_role("admin"))])
    """
    allowed = {
        r.value if isinstance(r, UserRole) else str(r) for r in roles
    }

    async def _guard(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions for this action",
            )
        return current_user

    return _guard
