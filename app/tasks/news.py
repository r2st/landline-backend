"""Celery tasks for company news monitoring."""

import asyncio
import logging

from app.database_celery import celery_session
from app.services.news_monitor import get_news_monitor
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


@celery_app.task(name="app.tasks.news.check_company_news_batch")
def check_company_news_batch():
    """Periodic task to check news for tracked companies."""
    logger.info("Starting periodic company news check")

    async def _run():
        monitor = get_news_monitor()
        async with celery_session() as db:
            results = await monitor.check_all_companies(db, limit=5)
            total = sum(r["news_found"] for r in results)
            if total > 0:
                logger.info("News check: found %d articles across %d companies", total, len(results))
            return {"companies_with_news": len(results), "total_articles": total, "results": results}

    return _run_async(_run())
