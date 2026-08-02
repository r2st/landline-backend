"""Celery tasks for company news monitoring."""

import asyncio
import logging

from app.tasks.org_runner import for_each_org
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

        # Per-org: tracked companies are org-scoped.
        async def _body(db):
            results = await monitor.check_all_companies(db, limit=5)
            total = sum(r["news_found"] for r in results)
            if total > 0:
                logger.info("News check: found %d articles across %d companies", total, len(results))
            return {"companies_with_news": len(results), "total_articles": total, "results": results}

        return await for_each_org(_body, label="check_company_news_batch")

    return _run_async(_run())


@celery_app.task(name="app.tasks.news.enroll_on_company_signals")
def enroll_on_company_signals():
    """React to fresh company signals by surfacing un-enrolled prospects.

    Runs shortly after the news sweep. On a fresh funding/hire/expansion signal
    it finds the company's un-enrolled emailable contacts and flags them for
    priority outreach (or auto-enrolls them when the org opted in). Reads both
    ``CompanyNews`` and the scored ``IntentSignal`` table. See
    :func:`app.services.signal_enrollment.surface_prospects_on_signals`.
    """
    from app.config import get_settings

    settings = get_settings()
    if not settings.signal_enrollment_enabled:
        logger.info("Signal enrollment disabled (signal_enrollment_enabled=False)")
        return {"status": "disabled"}

    logger.info("Scanning fresh company signals for priority outreach")

    async def _run():
        from app.services.signal_enrollment import surface_prospects_on_signals

        # Per-org: news, contacts, enrollments and follow-ups are all org-scoped.
        async def _body(db):
            return await surface_prospects_on_signals(
                db,
                fresh_hours=settings.signal_enrollment_fresh_hours,
                min_relevance=settings.signal_enrollment_min_relevance,
                auto_enroll=settings.signal_enrollment_auto_enroll,
                include_intent=settings.signal_enrollment_include_intent,
                min_intent_score=settings.signal_enrollment_min_intent_score,
            )

        return await for_each_org(_body, label="enroll_on_company_signals")

    return _run_async(_run())
