"""Celery task for the daily AI action plan (Feature 1.6).

``generate_daily_suggestions`` runs the smart-suggestions engine and persists the
ranked recommendations as ``SuggestionOutcome`` rows with status ``suggested``
(attributed to the recommended rep when known). This gives each rep a
prioritized daily action plan and seeds the feedback loop that later records
whether each suggestion was acted on.
"""

import asyncio
import logging
import uuid

from app.tasks.org_runner import for_each_org
from app.models.suggestion_outcome import SuggestionOutcome
from app.services.smart_suggestions import generate_ai_suggestions
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def generate_daily_suggestions_for_session(db, *, limit: int = 25, use_ai: bool = True) -> dict:
    """Core logic (testable with any session): generate + persist today's plan."""
    result = await generate_ai_suggestions(db, limit=limit, use_ai=use_ai)
    created = 0
    for s in result["suggestions"]:
        rep_id = s.get("recommended_rep_id")
        company_id = s.get("company_id")
        db.add(SuggestionOutcome(
            user_id=uuid.UUID(rep_id) if rep_id else None,
            suggestion_key=s["id"],
            suggestion_type=s["type"],
            status="suggested",
            action=s.get("action"),
            company_id=uuid.UUID(company_id) if company_id else None,
            payload=s,
        ))
        created += 1
    logger.info("Generated %d daily suggestions (source=%s)", created, result["source"])
    return {"created": created, "source": result["source"]}


@celery_app.task(name="app.tasks.smart_suggestions_task.generate_daily_suggestions")
def generate_daily_suggestions(use_ai: bool = True):
    """Beat task: produce and persist the prioritized daily action plan."""
    async def _run():
        # Per-org: the daily action plan / suggestions are org-scoped.
        async def _body(db):
            try:
                result = await generate_daily_suggestions_for_session(db, use_ai=use_ai)
                await db.commit()
                return result
            except Exception:
                await db.rollback()
                raise

        return await for_each_org(_body, label="generate_daily_suggestions")

    try:
        return _run_async(_run())
    except Exception as exc:
        logger.error("generate_daily_suggestions failed: %s", str(exc))
        return {"status": "error", "error": str(exc)}
