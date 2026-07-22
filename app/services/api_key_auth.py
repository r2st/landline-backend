"""Authentication + rate limiting for the public REST API.

This is the seam between a raw ``X-API-Key`` header and an org-scoped request:

1. :func:`generate_api_key` mints a new raw key (shown once) and returns the
   pieces to persist (hash + prefix).
2. :func:`get_api_key_context` is the FastAPI dependency every public endpoint
   runs through. It resolves the presented key to an :class:`ApiKey` row, binds
   that key's org as the request context (so the global read filter and
   per-insert ``org_id`` stamping isolate everything to the key's tenant),
   enforces the per-key rate limit, and records ``last_used_at``.
3. :func:`require_scope` builds a dependency that gates an endpoint on one of
   the key's granted scopes.

The key lookup runs with **no org bound** (auth is what establishes the org), so
the global ``org_id`` read filter is a no-op for it — a presented key is found by
its hash regardless of tenant, then its own ``org_id`` becomes the scope for the
rest of the request.

Rate limiting uses Redis (fixed 60-second window, keyed per API key). If Redis is
unreachable it degrades to an in-process counter so the limit is still enforced
within a worker rather than failing open silently.
"""

from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.api_key import ApiKey, KEY_PREFIX, hash_key
from app.org_context import set_current_org_id

logger = logging.getLogger(__name__)

# The header carrying the public-API credential. Declared via APIKeyHeader (not a
# raw Header) so FastAPI advertises it as an ``apiKey`` security scheme in the
# public OpenAPI doc — the "Authorize" button on /api/v1/public/docs. auto_error
# is False so a missing header yields our own 401 (consistent JSON shape) rather
# than FastAPI's default 403.
API_KEY_HEADER_NAME = "X-API-Key"
_api_key_header = APIKeyHeader(name=API_KEY_HEADER_NAME, auto_error=False)

# How stale last_used_at may get before we write it again. Avoids a DB write on
# every single request while keeping the "last used" signal usefully fresh.
_LAST_USED_THROTTLE_SECONDS = 60


def generate_api_key() -> tuple[str, str, str]:
    """Mint a new API key.

    Returns ``(raw_key, key_hash, key_prefix)``:

    * ``raw_key`` — the full secret, e.g. ``llk_<43 url-safe chars>``. Shown to
      the caller exactly once and never persisted.
    * ``key_hash`` — SHA-256 hex digest to store and match against.
    * ``key_prefix`` — the leading, non-secret slice for display.
    """
    raw_key = KEY_PREFIX + secrets.token_urlsafe(32)
    return raw_key, hash_key(raw_key), raw_key[: len(KEY_PREFIX) + 6]


class ApiKeyContext:
    """The resolved public-API caller for one request.

    Carries the authenticated :class:`ApiKey`, its org, and granted scopes so
    downstream dependencies (``require_scope``) and handlers can authorize
    without re-querying.
    """

    def __init__(self, api_key: ApiKey) -> None:
        self.api_key = api_key
        self.org_id = api_key.org_id
        self.scopes = list(api_key.scopes or [])

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": API_KEY_HEADER_NAME},
    )


# --- Rate limiting -----------------------------------------------------------

# In-process fallback counter: {window_key: (count, window_expiry_epoch)}. Only
# used when Redis is unavailable; pruned lazily as windows expire.
_local_counters: dict[str, tuple[int, float]] = {}

# Lazily-created, cached async Redis client (mirrors app.services.cache). Disabled
# on first failure so a persistent outage doesn't retry a dead socket per request.
_redis = None
_redis_disabled = False


async def _get_redis():
    global _redis, _redis_disabled
    if _redis_disabled:
        return None
    if _redis is not None:
        return _redis
    try:
        import redis.asyncio as aioredis

        _redis = aioredis.from_url(
            get_settings().redis_url,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )
        return _redis
    except Exception as exc:  # noqa: BLE001 — any redis failure → local fallback
        logger.info("API rate-limit redis unavailable, using local counter: %s", exc)
        _redis_disabled = True
        return None


async def _redis_incr(window_key: str) -> int | None:
    """INCR ``window_key`` in Redis with a 60s TTL; return the new count.

    Returns ``None`` (not 0) when Redis is unavailable, so the caller can tell
    "Redis said this is request N" apart from "Redis is down, fall back".
    """
    client = await _get_redis()
    if client is None:
        return None
    try:
        count = await client.incr(window_key)
        if count == 1:
            await client.expire(window_key, 60)
        return int(count)
    except Exception as exc:  # noqa: BLE001 — any redis failure → local fallback
        logger.debug("API rate-limit redis incr failed, using local counter: %s", exc)
        return None


def _local_incr(window_key: str, now: float) -> int:
    """INCR ``window_key`` in the in-process fallback counter."""
    # Prune expired windows so the dict can't grow unbounded across minutes.
    for k in [k for k, (_, exp) in _local_counters.items() if exp <= now]:
        _local_counters.pop(k, None)
    count, exp = _local_counters.get(window_key, (0, now + 60))
    count += 1
    _local_counters[window_key] = (count, exp)
    return count


async def enforce_rate_limit(api_key: ApiKey) -> None:
    """Raise 429 if this key has exceeded ``rate_limit`` in the current minute.

    Fixed-window: the window key includes the current minute, so the counter
    resets each minute. Redis-backed for cross-worker correctness, with an
    in-process fallback so a Redis outage throttles per-worker rather than
    removing the limit entirely.
    """
    limit = api_key.rate_limit or 0
    if limit <= 0:
        return  # 0/none → unlimited (defensive; keys default to a real budget)

    now = time.time()
    window = int(now // 60)
    window_key = f"apikey_rl:{api_key.id}:{window}"

    count = await _redis_incr(window_key)
    if count is None:
        count = _local_incr(window_key, now)

    if count > limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded ({limit}/minute for this API key)",
            headers={"Retry-After": str(60 - int(now % 60))},
        )


# --- last_used bookkeeping ---------------------------------------------------


async def touch_last_used(db: AsyncSession, api_key: ApiKey) -> None:
    """Update ``last_used_at`` at most once per throttle window."""
    now = datetime.now(timezone.utc)
    last = api_key.last_used_at
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if last is None or (now - last).total_seconds() >= _LAST_USED_THROTTLE_SECONDS:
        api_key.last_used_at = now
        await db.flush()


# --- The dependency ----------------------------------------------------------


async def get_api_key_context(
    presented_key: str | None = Depends(_api_key_header),
    db: AsyncSession = Depends(get_db),
) -> ApiKeyContext:
    """Authenticate the ``X-API-Key`` header and bind the caller's org.

    Fails closed with 401 for a missing, unknown, revoked, or expired key, and
    429 when the per-key rate limit is exceeded. On success the key's org is
    bound as the request context for the remainder of the request.
    """
    if not presented_key:
        raise _unauthorized("Missing API key")

    # Look the key up by hash. No org is bound yet, so the global org read filter
    # does not apply here — the key is found regardless of tenant, then its own
    # org scopes everything that follows.
    key_hash = hash_key(presented_key)
    api_key = (
        await db.execute(select(ApiKey).where(ApiKey.key_hash == key_hash))
    ).scalar_one_or_none()

    if api_key is None:
        raise _unauthorized("Invalid API key")
    if api_key.is_revoked:
        raise _unauthorized("API key has been revoked")
    if api_key.is_expired:
        raise _unauthorized("API key has expired")

    # Per-key rate limit before doing any real work.
    await enforce_rate_limit(api_key)

    # Bind the key's org for the rest of the request: drives per-insert org_id
    # stamping and the global read filter (see app.org_context / models.mixins).
    set_current_org_id(api_key.org_id)

    await touch_last_used(db, api_key)

    return ApiKeyContext(api_key)


def require_scope(scope: str):
    """Build a dependency that requires the caller's key to hold ``scope``.

    Usage: ``ctx: ApiKeyContext = Depends(require_scope("contacts:read"))``.
    Returns the :class:`ApiKeyContext` so an endpoint gets auth + scope check in
    a single dependency. 403 when the scope is absent.
    """

    async def _check(ctx: ApiKeyContext = Depends(get_api_key_context)) -> ApiKeyContext:
        if not ctx.has_scope(scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"API key is missing the required scope: {scope}",
            )
        return ctx

    return _check
