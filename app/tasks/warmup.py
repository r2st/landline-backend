"""Celery task: daily warm-up progress update.

Steps every sending account's :class:`~app.models.sender_warmup.SenderWarmupPlan`
forward one day — recomputing ``current_day`` from ``start_date`` and re-deriving
the ramped ``daily_limit`` — so the send-path warm-up gate always reads today's
cap. Runs once per active org (plans are org-scoped).
"""

import asyncio
import logging

from app.services import warmup
from app.tasks.celery_app import celery_app
from app.tasks.org_runner import for_each_org

logger = logging.getLogger(__name__)


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(name="app.tasks.warmup.advance_warmup_progress")
def advance_warmup_progress():
    """Advance every warm-up plan by one day (per active org)."""
    logger.info("Advancing sender warm-up plans")

    async def _run():
        async def _body(db):
            result = await warmup.advance_all(db)
            logger.info(
                "Warm-up advanced: %d plans, %d changed, %d graduated",
                result.get("plans", 0), result.get("advanced", 0), result.get("graduated", 0),
            )
            return result

        return await for_each_org(_body, label="advance_warmup_progress")

    try:
        return _run_async(_run())
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("advance_warmup_progress failed: %s", str(exc))
        return {"status": "error", "error": str(exc)}
