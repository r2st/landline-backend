"""Celery task for Smart Send-Time Optimization (Feature 6).

Nightly (via Beat) on the ``enrichment`` queue: recompute every enrolled
contact's engagement profile (local (dow, hour) buckets, best day/hour,
confidence) and resolve/backfill their timezone. The per-step scheduling
decision itself runs INSIDE ``process_outreach_queue`` (it sets
``scheduled_send_at`` when a step comes due) — keeping timing next to sending —
so there is deliberately no separate scheduling task here.

The task stays thin; all the aggregation/resolution logic lives in
``send_time_optimizer``.
"""

import asyncio
import logging

from app.tasks.org_runner import for_each_org
from app.services.send_time_optimizer import get_send_time_optimizer
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(name="app.tasks.send_timing.refresh_engagement_profiles")
def refresh_engagement_profiles():
    """Recompute per-contact engagement buckets + backfill timezones (nightly)."""
    logger.info("Refreshing contact engagement profiles for send-time optimization")

    async def _run():
        optimizer = get_send_time_optimizer()

        # Per-org: contacts and their engagement profiles are org-scoped.
        async def _body(db):
            report = await optimizer.refresh_profiles(db)
            await db.commit()
            logger.info(
                "Engagement profiles refreshed: %d updated, %d timezones backfilled",
                report.get("profiles_updated", 0),
                report.get("timezones_backfilled", 0),
            )
            return report

        return await for_each_org(_body, label="refresh_engagement_profiles")

    return _run_async(_run())
