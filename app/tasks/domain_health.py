"""Celery task for the Domain Health Dashboard (Feature 5).

Runs daily (via Beat) on the ``enrichment`` queue. DNS/DNSBL data changes slowly
and over-querying the DNSBL zones can get you rate-limited or banned, so a daily
cadence is both sufficient and polite. The task stays thin — all the SPF/DKIM/
DMARC + DNSBL + scoring + upsert + change-alert logic lives in
``domain_health_service``.
"""

import asyncio
import logging

from app.database_celery import celery_session
from app.services.domain_health_service import get_domain_health_checker
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(name="app.tasks.domain_health.check_domain_health")
def check_domain_health():
    """Daily SPF/DKIM/DMARC + blacklist sweep across all sending domains."""
    logger.info("Checking domain health across all sending domains")

    async def _run():
        checker = get_domain_health_checker()
        async with celery_session() as db:
            report = await checker.check_all(db)
            await db.commit()
            at_risk = [r for r in report.get("results", []) if r["band"] == "at_risk"]
            if at_risk:
                logger.warning("%d domain(s) at risk: %s",
                               len(at_risk), [r["domain"] for r in at_risk])
            return report

    return _run_async(_run())
