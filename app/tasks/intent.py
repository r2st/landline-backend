"""Celery tasks for buyer-intent signal detection (feature 2.1)."""

import asyncio
import logging

from app.services.intent_monitor import get_intent_monitor
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


@celery_app.task(name="app.tasks.intent.scan_intent_signals")
def scan_intent_signals(limit: int = 15):
    """Daily scan of active-pipeline companies for buyer-intent signals."""
    logger.info("Starting buyer-intent signal scan")

    async def _run():
        monitor = get_intent_monitor()

        # Per-org: companies scanned for intent signals are org-scoped.
        async def _body(db):
            results = await monitor.scan_all_companies(db, limit=limit)
            total = sum(r["signals_found"] for r in results)
            if total > 0:
                logger.info(
                    "Intent scan: found %d signals across %d companies", total, len(results)
                )
            return {"companies_with_signals": len(results), "total_signals": total, "results": results}

        return await for_each_org(_body, label="scan_intent_signals")

    return _run_async(_run())
