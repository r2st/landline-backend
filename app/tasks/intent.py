"""Celery tasks for buyer-intent signal detection (feature 2.1)."""

import asyncio
import logging

from app.database_celery import celery_session
from app.services.intent_monitor import get_intent_monitor
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _run_async(coro):
    """Run async code in Celery's sync context with a fresh event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(name="app.tasks.intent.scan_intent_signals")
def scan_intent_signals(limit: int = 15):
    """Daily scan of active-pipeline companies for buyer-intent signals."""
    logger.info("Starting buyer-intent signal scan")

    async def _run():
        monitor = get_intent_monitor()
        async with celery_session() as db:
            results = await monitor.scan_all_companies(db, limit=limit)
            total = sum(r["signals_found"] for r in results)
            if total > 0:
                logger.info(
                    "Intent scan: found %d signals across %d companies", total, len(results)
                )
            return {"companies_with_signals": len(results), "total_signals": total, "results": results}

    return _run_async(_run())
