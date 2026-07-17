"""Celery tasks for competitive intelligence (feature 2.6)."""

import asyncio
import logging

from app.services.competitive_intelligence import get_competitive_intelligence
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


@celery_app.task(name="app.tasks.competitive.monitor_competitor_news")
def monitor_competitor_news(limit: int = 10):
    """Periodic: check the news for each tracked competitor and record mentions."""
    logger.info("Starting competitor news monitoring")

    async def _run():
        ci = get_competitive_intelligence()

        # Per-org: tracked competitors are org-scoped.
        async def _body(db):
            results = await ci.monitor_competitor_news(db, limit=limit)
            total = sum(r["news_mentions"] for r in results)
            if total > 0:
                logger.info(
                    "Competitor news: %d new mentions across %d competitors", total, len(results)
                )
            return {"competitors_with_news": len(results), "total_mentions": total, "results": results}

        return await for_each_org(_body, label="monitor_competitor_news")

    return _run_async(_run())
