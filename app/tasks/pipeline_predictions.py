"""Celery task for predictive pipeline scoring (Feature 1.1).

``rescore_deal_predictions`` trains the forecast model on decided deals and
(re)scores every active deal, storing a fresh ``DealPrediction`` per deal. Runs
weekly on the beat schedule and on-demand (e.g. after a deal changes stage).
"""

import asyncio
import logging

from app.database_celery import celery_session
from app.services.pipeline_forecaster import rescore_active_deals
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


@celery_app.task(name="app.tasks.pipeline_predictions.rescore_deal_predictions")
def rescore_deal_predictions(use_ai: bool = True):
    """Beat task: retrain the forecaster and rescore all active deals."""
    logger.info("Rescoring deal predictions")

    async def _run():
        async with celery_session() as db:
            try:
                result = await rescore_active_deals(db, use_ai=use_ai)
                await db.commit()
                return result
            except Exception:
                await db.rollback()
                raise

    try:
        return _run_async(_run())
    except Exception as exc:
        logger.error("rescore_deal_predictions failed: %s", str(exc))
        return {"status": "error", "error": str(exc)}
