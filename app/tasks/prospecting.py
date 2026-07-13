"""Celery tasks for the autonomous prospecting agent (feature 3.1)."""

import asyncio
import logging

from app.database_celery import celery_session
from app.services.autonomous_prospector import get_prospector
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(name="app.tasks.prospecting.run_prospecting_agent")
def run_prospecting_agent(target_volume: int | None = None):
    """Run one autonomous prospecting session (scheduled daily)."""
    logger.info("Starting autonomous prospecting run")

    async def _run():
        prospector = get_prospector()
        async with celery_session() as db:
            run = await prospector.run(db, target_volume=target_volume, trigger="scheduled")
            await db.commit()
            return {
                "run_id": str(run.id),
                "status": run.status,
                "found": run.companies_found,
                "qualified": run.companies_qualified,
                "enrolled": run.companies_enrolled,
                "review_queue": run.companies_review_queue,
            }

    return _run_async(_run())


@celery_app.task(name="app.tasks.prospecting.relearn_icp")
def relearn_icp():
    """Weekly: refit the Ideal Customer Profile from won-deal characteristics."""
    async def _run():
        prospector = get_prospector()
        async with celery_session() as db:
            icp = await prospector.learn_icp(db)
            await db.commit()
            return {"icp_id": str(icp.id), "learned_from_deals": icp.learned_from_deal_count}

    return _run_async(_run())
