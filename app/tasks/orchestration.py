"""Celery task for autonomous multi-channel orchestration (feature 2.2)."""

import asyncio
import logging

from app.database_celery import celery_session
from app.services.orchestrator import get_orchestrator
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(name="app.tasks.orchestration.run_orchestrator")
def run_orchestrator(limit: int = 50):
    """Process all autonomous-mode enrollments: decide + record the next touch."""
    logger.info("Starting autonomous orchestration run")

    async def _run():
        orch = get_orchestrator()
        async with celery_session() as db:
            results = await orch.run_all_autonomous(db, limit=limit)
            actions = {}
            for r in results:
                actions[r["action"]] = actions.get(r["action"], 0) + 1
            if results:
                logger.info(
                    "Orchestration run: %d enrollments processed (%s)", len(results), actions
                )
            return {"processed": len(results), "actions": actions, "results": results}

    return _run_async(_run())
