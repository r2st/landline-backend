"""Celery task: auto-evaluate A/B tests and declare winners (Feature 3).

Runs daily via Beat. For each running test it feeds the per-variant stats into
the existing statistical engine (leader-vs-control z-test) and, when a variant
is significant at the test's confidence target with enough sample — or the hard
cap (500 sends/variant or 21 days) forces it — declares the winner, promotes it
to the step's active template, and concludes the test.

Deliberately thin: all logic lives in :mod:`app.services.ab_test_resolver`.
Never raises — an evaluation bug must not crash the worker.
"""

import asyncio
import logging

from sqlalchemy import select

from app.models.ab_test import AbTest
from app.services.ab_test_resolver import evaluate_test
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


@celery_app.task(name="app.tasks.ab_testing.evaluate_ab_tests")
def evaluate_ab_tests():
    """Evaluate every running A/B test; declare + promote winners when reached."""

    async def _run():
        try:
            # Per-org: A/B tests and their step templates are org-scoped.
            async def _body(db):
                tests = list(
                    (
                        await db.execute(
                            select(AbTest).where(AbTest.status != "completed")
                        )
                    ).scalars().all()
                )
                concluded = 0
                for test in tests:
                    result = await evaluate_test(db, test)
                    if result.get("status") == "completed":
                        concluded += 1
                        logger.info(
                            "A/B test %s concluded: winner=%s default=%s conf=%s",
                            test.id,
                            result.get("winner_variant_id"),
                            result.get("default_winner"),
                            result.get("confidence"),
                        )
                await db.commit()
                return {"evaluated": len(tests), "concluded": concluded}

            return await for_each_org(_body, label="evaluate_ab_tests")
        except Exception as exc:  # never raise out of a scheduled evaluation
            logger.exception("evaluate_ab_tests failed")
            return {"error": str(exc)}

    return _run_async(_run())
