"""Celery tasks for AI sales coaching (feature 2.4)."""

import asyncio
import logging

from sqlalchemy import select

from app.dependencies import scope_users_to_current_org
from app.models.user import User
from app.services.sales_coach import compute_team_benchmarks, generate_rep_performance, period_bounds
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


@celery_app.task(name="app.tasks.coaching.generate_weekly_coaching")
def generate_weekly_coaching(period_type: str = "weekly"):
    """Weekly: generate a performance + coaching snapshot for every active rep.

    Team benchmarks are computed once and reused across reps so a single
    weekly run doesn't recompute the whole team's aggregates N times.
    """
    logger.info("Starting weekly coaching generation")

    async def _run():
        # Per-org: reps (users) and their performance data are org-scoped.
        async def _body(db):
            period_start, period_end = period_bounds(period_type)
            benchmarks = await compute_team_benchmarks(db, period_start, period_end)

            # ``for_each_org`` has bound this iteration's org, but User is not
            # org-scoped, so an unscoped select here read every user on the
            # platform on *every* pass — writing N-orgs × N-users snapshots, each
            # stamped with whichever tenant the loop was on.
            reps = (
                await db.execute(
                    scope_users_to_current_org(
                        select(User).where(User.is_active.is_(True))
                    )
                )
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

        return await for_each_org(_body, label="generate_weekly_coaching")

    return _run_async(_run())
