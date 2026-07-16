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

# Sensible defaults; can be overridden per-endpoint at the decorator.
AUTH_RATE_LIMIT = "10/minute"      # login / refresh — throttle credential stuffing
AI_RATE_LIMIT = "30/minute"        # AI endpoints — throttle expensive LLM calls

# headers_enabled must stay False: slowapi injects the X-RateLimit-* headers by
# reaching for a ``response: Response`` kwarg on the endpoint, and raises if it
# isn't there. Endpoints returning a pydantic model (the norm here) don't declare
# one, so enabling this 500s every *successful* call while failures still return
# cleanly — a login that rejects wrong passwords with 401 but 500s on the right
# one. Limits themselves are enforced independently of these headers.
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=os.getenv("RATELIMIT_STORAGE_URI", "memory://"),
    headers_enabled=False,
)
