"""Short-TTL response cache for hot dashboard/analytics aggregates.

These endpoints run several GROUP BY / COUNT queries per request and are polled
frequently by the dashboard, but their answers only need to be seconds-fresh.
Caching the computed result in Redis for a short TTL turns a burst of identical
requests into one query pass.

Design notes:

* **Graceful degradation** — if Redis is unconfigured/unreachable, or caching is
  disabled (tests), every call just runs the builder. The cache never changes a
  response, only how often it's recomputed.
* **Tenant isolation** — keys are scoped by the current org id, so one tenant can
  never be served another tenant's cached aggregate.
* **Fail-open on errors** — any Redis error falls back to computing fresh.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Optional

from app.config import get_settings
from app.org_context import get_current_org_id

logger = logging.getLogger(__name__)

_redis = None
_disabled = False


async def _get_redis():
    """Lazily create the async Redis client; disable on first failure."""
    global _redis, _disabled
    if _disabled or not get_settings().response_cache_enabled:
        return None
    if _redis is not None:
        return _redis
    try:
        import redis.asyncio as aioredis

        _redis = aioredis.from_url(
            get_settings().redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )
        return _redis
    except Exception as exc:  # pragma: no cover - defensive
        logger.info("Response cache disabled (redis unavailable): %s", exc)
        _disabled = True
        return None


def _scoped_key(key: str) -> str:
    org = get_current_org_id()
    return f"rcache:{org or 'global'}:{key}"


async def get_json(key: str) -> Optional[Any]:
    r = await _get_redis()
    if r is None:
        return None
    try:
        raw = await r.get(_scoped_key(key))
        return json.loads(raw) if raw else None
    except Exception as exc:  # pragma: no cover - fail open
        logger.debug("cache get failed for %s: %s", key, exc)
        return None


async def set_json(key: str, value: Any, ttl: int) -> None:
    r = await _get_redis()
    if r is None:
        return
    try:
        await r.set(_scoped_key(key), json.dumps(value, default=str), ex=ttl)
    except Exception as exc:  # pragma: no cover - fail open
        logger.debug("cache set failed for %s: %s", key, exc)


async def cached(
    key: str,
    builder: Callable[[], Awaitable[Any]],
    ttl: Optional[int] = None,
) -> Any:
    """Return the cached value for ``key`` or compute + store it.

    ``builder`` is an async callable returning either a Pydantic model or a
    JSON-serialisable value. On a cache hit the stored (JSON) form is returned —
    FastAPI's ``response_model`` re-validates it, so the endpoint's public shape
    is unchanged.
    """
    hit = await get_json(key)
    if hit is not None:
        return hit
    result = await builder()
    payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
    await set_json(key, payload, ttl if ttl is not None else get_settings().response_cache_ttl)
    return result
