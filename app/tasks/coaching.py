"""Celery tasks for AI sales coaching (feature 2.4)."""

import asyncio
import logging

from sqlalchemy import select

from app.database_celery import celery_session
from app.models.user import User
from app.services.sales_coach import compute_team_benchmarks, generate_rep_performance, period_bounds
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(name="app.tasks.coaching.generate_weekly_coaching")
def generate_weekly_coaching(period_type: str = "weekly"):
    """Weekly: generate a performance + coaching snapshot for every active rep.

    Team benchmarks are computed once and reused across reps so a single
    weekly run doesn't recompute the whole team's aggregates N times.
    """
    logger.info("Starting weekly coaching generation")

    async def _run():
        async with celery_session() as db:
            period_start, period_end = period_bounds(period_type)
            benchmarks = await compute_team_benchmarks(db, period_start, period_end)

            reps = (
                await db.execute(select(User).where(User.is_active.is_(True)))
            ).scalars().all()

            generated = 0
            for rep in reps:
                try:
                    await generate_rep_performance(
                        db, rep, period_type=period_type, benchmarks=benchmarks
                    )
                    generated += 1
                except Exception as e:  # never let one rep kill the batch
                    logger.error("Coaching generation failed for %s: %s", rep.email, str(e))
            await db.commit()
            logger.info("Weekly coaching: generated %d rep snapshots", generated)
            return {"reps_processed": generated, "period_type": period_type}

    return _run_async(_run())
