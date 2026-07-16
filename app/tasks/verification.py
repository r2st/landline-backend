"""Celery tasks for Hunter.io email verification (P3).

Two sweeps, both on the ``enrichment`` queue, both fully bounded by the daily
credit budget:

* ``verify_pending_contacts`` (every 15 min) — verify contacts that have never
  reached a verdict, so a freshly-imported/prospected contact is verified well
  before its sequence's first send. Idempotent via ``email_verified_at``.
* ``reverify_stale_contacts`` (weekly) — re-verify addresses that have aged past
  ``hunter_reverify_days``. People leave; addresses rot.

Both no-op when no Hunter key is configured — a missing key means the capability
is off, and the pipeline keeps running on the free MX check exactly as before.
All the layering / mapping / fail-open logic lives in ``hunter_verification``;
these stay thin.
"""

import asyncio
import logging
from collections import Counter

from app.database_celery import celery_session
from app.services.hunter_verification import get_hunter_service
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

# Per-run ceilings. The credit budget is the real bound; these just keep any one
# run inside the 300s soft time limit.
_PENDING_BATCH_LIMIT = 200
_STALE_BATCH_LIMIT = 200


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _summarize(results: list[dict]) -> dict:
    """Collapse per-contact verdicts into a breakdown for the task result.

    Keys on the verdict where one was reached, else the call status — so a run
    that was rate-limited reads differently from a run that found everything
    deliverable.
    """
    return dict(Counter(r["result"] or r["status"] for r in results))


async def _sweep(select_fn, limit: int, label: str) -> dict:
    service = get_hunter_service()
    if not service.enabled:
        logger.info("%s: no Hunter API key configured — skipping", label)
        return {"status": "skipped", "reason": "no_api_key", "checked": 0}

    async with celery_session() as db:
        budget = await service.make_budget(db)
        if budget.exhausted:
            logger.warning("%s: daily Hunter credit budget exhausted", label)
            return {"status": "skipped", "reason": "budget_exhausted", "checked": 0}

        # Only select what the budget can pay for — selecting 200 rows to skip
        # 190 of them would look like a completed sweep in the logs.
        capped = budget.cap(limit)
        contacts = await select_fn(db, capped)
        if not contacts:
            return {"status": "ok", "checked": 0, "breakdown": {}}

        results = await service.verify_batch(db, contacts, budget=budget)
        await db.commit()

        breakdown = _summarize(results)
        logger.info(
            "%s: verified %d contact(s) — %s (budget left: %s)",
            label, len(results), breakdown, budget.remaining,
        )
        return {
            "status": "ok",
            "checked": len(results),
            "breakdown": breakdown,
            "budget_remaining": (
                None if budget.remaining == float("inf") else int(budget.remaining)
            ),
        }


@celery_app.task(name="app.tasks.verification.verify_pending_contacts")
def verify_pending_contacts():
    """Verify never-verified contacts (every 15 min)."""

    async def _run():
        service = get_hunter_service()
        return await _sweep(
            service.select_pending, _PENDING_BATCH_LIMIT, "verify_pending_contacts"
        )

    return _run_async(_run())


@celery_app.task(name="app.tasks.verification.reverify_stale_contacts")
def reverify_stale_contacts():
    """Re-verify contacts whose verification has gone stale (weekly)."""

    async def _run():
        service = get_hunter_service()
        return await _sweep(
            service.select_stale, _STALE_BATCH_LIMIT, "reverify_stale_contacts"
        )

    return _run_async(_run())
