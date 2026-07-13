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

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=os.getenv("RATELIMIT_STORAGE_URI", "memory://"),
    headers_enabled=True,
)
