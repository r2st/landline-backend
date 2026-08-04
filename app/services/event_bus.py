"""Event bus for the AI Workflow Builder (feature 4.3).

Internal events are the trigger surface for workflows. :meth:`EventBus.publish`
does two things for every event:

1. **Streams it to Redis** (``XADD`` on the ``landline:events`` stream) so
   external consumers / a dedicated worker can process it. This is best-effort:
   if Redis is unconfigured or unreachable, streaming is skipped silently.

2. **Dispatches it in-process** to the :mod:`workflow_engine`, which finds
   matching workflows and executes them against the current DB session. This is
   the synchronous path that powers the API's ``emit`` endpoint and the test
   suite — no running Redis consumer required.

The Redis client is created lazily and cached; a connection failure disables the
streaming path for the rest of the process rather than retrying on every event.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings

logger = logging.getLogger(__name__)

REDIS_STREAM_KEY = "landline:events"
_MAXLEN = 10_000  # approximate cap on retained stream entries


class EventBus:
    """Publishes internal events to Redis Streams and to the workflow engine."""

    def __init__(self) -> None:
        self._redis = None
        self._redis_disabled = False

    async def _get_redis(self):
        """Lazily create the async Redis client; disable on first failure."""
        if self._redis_disabled:
            return None
        if self._redis is not None:
            return self._redis
        try:
            import redis.asyncio as aioredis

            settings = get_settings()
            self._redis = aioredis.from_url(
                settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
                # Bounded, like every other Redis client in this codebase
                # (``send_limiter``, ``send_pacing``, ``cache``, ``api_key_auth``).
                # This one had no timeouts, which made "streaming is best-effort"
                # untrue in the case that matters: an unreachable *host* fails
                # fast, but a host that accepts the TCP connection and then stops
                # answering — a wedged Redis, a firewall blackholing an
                # established connection, a failover mid-flight — leaves ``ping``
                # and ``xadd`` waiting on a socket with no deadline. And this is
                # not a background path: ``publish`` is awaited inline by the
                # workflow ``emit`` endpoint and by every internal event a
                # request raises, so the optional half of the work is what hangs
                # the request.
                socket_connect_timeout=0.5,
                socket_timeout=0.5,
            )
            # Verify connectivity once up front.
            await self._redis.ping()
            return self._redis
        except Exception as exc:  # noqa: BLE001 — streaming is optional
            logger.info("Event bus: Redis streaming disabled (%s)", exc)
            self._redis_disabled = True
            self._redis = None
            return None

    async def _stream(self, event_type: str, payload: dict, org_id: Optional[uuid.UUID]) -> Optional[str]:
        """Best-effort XADD; returns the stream entry id or None."""
        redis = await self._get_redis()
        if redis is None:
            return None
        try:
            entry = {
                "event_type": event_type,
                "org_id": str(org_id) if org_id else "",
                "payload": json.dumps(payload, default=str),
            }
            return await redis.xadd(REDIS_STREAM_KEY, entry, maxlen=_MAXLEN, approximate=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Event bus XADD failed, disabling streaming: %s", exc)
            self._redis_disabled = True
            return None

    async def publish(
        self,
        db: AsyncSession,
        event_type: str,
        payload: dict,
        org_id: Optional[uuid.UUID] = None,
    ) -> dict:
        """Stream the event and run any workflows it triggers.

        Returns a summary: the stream id (if any) and the workflow-dispatch
        result (workflows matched and run outcomes).
        """
        stream_id = await self._stream(event_type, payload, org_id)

        # Import here to avoid a circular import at module load
        # (workflow_engine imports actions that import services).
        from app.services.workflow_engine import get_workflow_engine

        dispatch = await get_workflow_engine().dispatch_event(
            db, event_type, payload, org_id
        )
        return {
            "event_type": event_type,
            "stream_id": stream_id,
            "streamed": stream_id is not None,
            **dispatch,
        }

    async def close(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._redis = None


_event_bus: Optional[EventBus] = None


def get_event_bus() -> EventBus:
    global _event_bus
    if _event_bus is None:
        _event_bus = EventBus()
    return _event_bus
