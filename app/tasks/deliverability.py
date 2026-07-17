"""Celery task for sender deliverability health (feature 3.3)."""

import asyncio
import logging

from app.services.deliverability_optimizer import get_deliverability_optimizer
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


@celery_app.task(name="app.tasks.deliverability.assess_sender_health")
def assess_sender_health():
    """Hourly reputation assessment across all sending accounts."""
    logger.info("Assessing sender deliverability health")

    async def _run():
        optimizer = get_deliverability_optimizer()

        # Per-org: sending accounts and their reputation are org-scoped.
        async def _body(db):
            results = await optimizer.assess_all(db)
            await db.commit()
            throttled = [r for r in results if r["status"] in ("throttled", "critical")]
            if throttled:
                logger.warning("%d sender account(s) throttled: %s",
                               len(throttled), [r["email"] for r in throttled])
            return {"accounts_assessed": len(results), "throttled": len(throttled), "results": results}

        return await for_each_org(_body, label="assess_sender_health")

    return _run_async(_run())
