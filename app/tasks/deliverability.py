"""Celery task for sender deliverability health (feature 3.3)."""

import asyncio
import logging

from app.database_celery import celery_session
from app.services.deliverability_optimizer import get_deliverability_optimizer
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(name="app.tasks.deliverability.assess_sender_health")
def assess_sender_health():
    """Hourly reputation assessment across all sending accounts."""
    logger.info("Assessing sender deliverability health")

    async def _run():
        optimizer = get_deliverability_optimizer()
        async with celery_session() as db:
            results = await optimizer.assess_all(db)
            await db.commit()
            throttled = [r for r in results if r["status"] in ("throttled", "critical")]
            if throttled:
                logger.warning("%d sender account(s) throttled: %s",
                               len(throttled), [r["email"] for r in throttled])
            return {"accounts_assessed": len(results), "throttled": len(throttled), "results": results}

    return _run_async(_run())
