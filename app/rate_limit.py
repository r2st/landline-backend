"""Shared slowapi rate limiter.

Kept in its own module so routers can import the limiter without creating an
import cycle through ``app.main``. Endpoints opt in with the
``@limiter.limit(...)`` decorator and must take a ``request: Request`` param.

Rate limiting is keyed on the client IP. In-memory storage is used by default;
for a multi-process deployment set ``RATELIMIT_STORAGE_URI`` to the Redis URL so
limits are shared across workers.
"""

import os

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

# Sensible defaults; can be overridden per-endpoint at the decorator.
AUTH_RATE_LIMIT = "10/minute"      # login / refresh — throttle credential stuffing
AI_RATE_LIMIT = "30/minute"        # AI endpoints — throttle expensive LLM calls


def org_or_ip_key(request: Request) -> str:
    """Rate-limit key: the caller's organization, falling back to client IP.

    In a multi-tenant SaaS, limits should be per *tenant*, not per IP — a whole
    org behind one office NAT shouldn't share a single budget, and one noisy
    tenant shouldn't be throttled by another. The org is read from the Bearer
    token's ``org_id`` claim (decoded cheaply, no DB hit). Anonymous requests
    (login/register — which carry no token) fall back to the client IP, which is
    exactly what you want for credential-stuffing protection. Platform admins
    (``org_id`` is null) also fall back to IP.
    """
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            from app.security import decode_token

            org_id = decode_token(auth[7:]).get("org_id")
            if org_id:
                return f"org:{org_id}"
        except Exception:  # invalid/expired token → fall back to IP
            pass
    return get_remote_address(request)

# headers_enabled must stay False: slowapi injects the X-RateLimit-* headers by
# reaching for a ``response: Response`` kwarg on the endpoint, and raises if it
# isn't there. Endpoints returning a pydantic model (the norm here) don't declare
# one, so enabling this 500s every *successful* call while failures still return
# cleanly — a login that rejects wrong passwords with 401 but 500s on the right
# one. Limits themselves are enforced independently of these headers.
limiter = Limiter(
    key_func=org_or_ip_key,
    storage_uri=os.getenv("RATELIMIT_STORAGE_URI", "memory://"),
    headers_enabled=False,
)
