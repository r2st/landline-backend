"""Inbox housekeeping tasks.

``clear_expired_snoozes`` nulls out ``inbox_thread_state.snoozed_until`` once it
has passed. Resurfacing itself is automatic — the inbox query only treats a
thread as snoozed while ``snoozed_until`` is in the future — so this sweep is
purely hygiene: it keeps the column from accumulating stale timestamps and gives
a single place to hang a "your snoozed thread is back" notification later.
"""

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import update

from app.models.inbox_thread_state import InboxThreadState
from app.tasks.celery_app import celery_app
from app.tasks.org_runner import for_each_org

logger = logging.getLogger(__name__)


def _run_async(coro):
    """Run async code in Celery's sync context with a fresh event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def clear_expired_snoozes_async(db, now: datetime | None = None) -> int:
    """Null out snoozes whose time has passed. Returns rows cleared."""
    now = now or datetime.now(timezone.utc)
    result = await db.execute(
        update(InboxThreadState)
        .where(
            InboxThreadState.snoozed_until.isnot(None),
            InboxThreadState.snoozed_until <= now,
        )
        .values(snoozed_until=None)
    )
    return result.rowcount or 0


@celery_app.task(name="app.tasks.inbox.clear_expired_snoozes")
def clear_expired_snoozes():
    """Periodic: clear elapsed inbox snoozes (every 5 minutes)."""

    async def _run():
        total = 0

        async def _body(db):
            nonlocal total
            cleared = await clear_expired_snoozes_async(db)
            await db.commit()
            total += cleared
            return cleared

        await for_each_org(_body, label="clear_expired_snoozes")
        return total

    try:
        cleared = _run_async(_run())
        if cleared:
            logger.info("clear_expired_snoozes cleared %s snooze(s)", cleared)
        return {"cleared": cleared}
    except Exception as exc:  # noqa: BLE001
        logger.error("clear_expired_snoozes failed: %s", str(exc))
        return {"cleared": 0, "error": str(exc)}
