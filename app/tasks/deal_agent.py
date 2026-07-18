"""Celery tasks for the Deal Progression Agent.

* ``scan_deal_pipeline`` — daily. Sweeps every org's open pipeline and records a
  :class:`~app.models.deal_progression_agent.DealAction` for each stalled deal.
* ``execute_pending_actions`` — hourly. Sends the follow-ups that have been
  approved (by a human via the API, or auto-approved for autonomous orgs), and
  expires actions on deals that have since moved on.

Both fan out with :func:`~app.tasks.org_runner.for_each_org` so every query runs
inside the right tenant's org context (deals / actions are org-scoped).
"""

import asyncio
import logging

from sqlalchemy import select

from app.models.deal import Deal
from app.models.deal_progression_agent import DealAction
from app.services import deal_progression_agent as agent
from app.tasks.celery_app import celery_app
from app.tasks.org_runner import for_each_org

logger = logging.getLogger(__name__)


def _run_async(coro):
    """Run async code in Celery's sync context with a fresh event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def execute_pending_actions_for_session(db) -> dict:
    """Send approved follow-ups and expire actions whose deal has moved on.

    Testable with any session + bound org context. Two passes:

    1. **Autonomy** — for an active org with ``auto_followup_enabled``, approve +
       send any still-``pending`` ``auto_followup`` action, then execute every
       ``approved`` follow-up (covers ones a human approved via the API too).
    2. **Housekeeping** — expire open actions on deals that are no longer
       ``pending`` (won/lost), so the queue reflects reality.
    """
    config = await agent.get_or_create_config(db)

    sent = 0
    expired = 0

    # 1. Execute approved follow-ups. For autonomous orgs, pending follow-ups are
    #    auto-approved first so they get sent without a human in the loop.
    statuses = ["approved"]
    if config.is_active and config.auto_followup_enabled:
        statuses.append("pending")

    followups = (
        await db.execute(
            select(DealAction).where(
                DealAction.action_type == "auto_followup",
                DealAction.status.in_(statuses),
            )
        )
    ).scalars().all()
    for action in followups:
        result = await agent.execute_action(db, action)
        if result.get("status") == "sent":
            sent += 1

    # 2. Expire open actions on deals that are no longer open.
    open_actions = (
        await db.execute(
            select(DealAction).where(DealAction.status.in_(("pending", "approved")))
        )
    ).scalars().all()
    for action in open_actions:
        deal = (
            await db.execute(select(Deal).where(Deal.id == action.deal_id))
        ).scalars().first()
        if deal is None or deal.status != "pending":
            action.status = "expired"
            expired += 1
    await db.flush()

    return {"sent": sent, "expired": expired, "candidates": len(followups)}


@celery_app.task(name="app.tasks.deal_agent.scan_deal_pipeline")
def scan_deal_pipeline():
    """Beat task (daily): scan every org's pipeline for stalled deals."""
    logger.info("Deal Progression Agent: scanning pipelines for stalled deals")

    async def _run():
        async def _body(db):
            try:
                result = await agent.scan_stale_deals(db)
                await db.commit()
                return result
            except Exception:
                await db.rollback()
                raise

        return await for_each_org(_body, label="scan_deal_pipeline")

    try:
        results = _run_async(_run())
        return {"status": "ok", "orgs": len(results), "results": results}
    except Exception as exc:
        logger.error("scan_deal_pipeline failed: %s", str(exc))
        return {"status": "error", "error": str(exc)}


@celery_app.task(name="app.tasks.deal_agent.execute_pending_actions")
def execute_pending_actions():
    """Beat task (hourly): send approved follow-ups, expire stale actions."""
    logger.info("Deal Progression Agent: executing pending actions")

    async def _run():
        async def _body(db):
            try:
                result = await execute_pending_actions_for_session(db)
                await db.commit()
                return result
            except Exception:
                await db.rollback()
                raise

        return await for_each_org(_body, label="execute_pending_actions")

    try:
        results = _run_async(_run())
        return {"status": "ok", "orgs": len(results), "results": results}
    except Exception as exc:
        logger.error("execute_pending_actions failed: %s", str(exc))
        return {"status": "error", "error": str(exc)}
