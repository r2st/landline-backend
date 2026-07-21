"""Celery task: daily Google Postmaster Tools metric pull (P2 audit).

Runs daily on the ``enrichment`` queue. Postmaster reports trailing daily
windows and the data moves slowly, so a daily cadence is both sufficient and
polite to the API quota. Per-org, because Postmaster accounts are org-scoped.
The service does all the work (OAuth refresh, list domains, fetch trafficStats,
upsert) and is fail-soft, so a missing/unconfigured account never fails the run.
"""

import asyncio
import logging

from app.services import postmaster_service
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


@celery_app.task(name="app.tasks.postmaster.pull_postmaster_metrics")
def pull_postmaster_metrics():
    """Pull daily reputation/spam/auth metrics for every connected domain."""
    logger.info("Pulling Google Postmaster Tools metrics")

    async def _run():
        async def _body(db):
            report = await postmaster_service.pull_all(db)
            await db.commit()
            if report.get("status") == "ok":
                logger.info(
                    "Postmaster pull: %d account(s), %d metric row(s)",
                    report.get("accounts", 0), report.get("rows", 0),
                )
            return report

        return await for_each_org(_body, label="pull_postmaster_metrics")

    return _run_async(_run())
