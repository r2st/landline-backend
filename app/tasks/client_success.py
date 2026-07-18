"""Celery tasks for the Client Success Agent.

Two beat tasks, both multi-tenant via :func:`for_each_org`:

* :func:`scan_client_health_task` — daily. For every org whose agent is active,
  scans client health and enqueues check-in / escalation / celebration /
  review-call actions (the deciding is in
  :mod:`app.services.client_success_agent`).
* :func:`execute_success_actions` — hourly. Marks approved auto check-ins as
  sent (the actual send is a downstream concern; here we record execution so
  the queue advances and the same action isn't re-processed).

Both bind each org's context before touching org-scoped rows, so isolation and
``org_id`` stamping are automatic.
"""

import asyncio
import logging

from app.services.client_success_agent import get_client_success_agent_service
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


@celery_app.task(name="app.tasks.client_success.scan_client_health_task")
def scan_client_health_task(limit: int = 500):
    """Daily per-org client-health scan → enqueue Client Success actions."""
    logger.info("Starting Client Success health scan")

    async def _run():
        async def _body(db):
            service = get_client_success_agent_service()
            result = await service.scan_client_health(db, limit=limit)
            await db.commit()
            return result

        return await for_each_org(_body, label="scan_client_health_task")

    results = _run_async(_run())
    total_actions = sum((r or {}).get("actions_created", 0) for r in results if r)
    logger.info(
        "Client Success scan complete: %s org(s), %s action(s) created",
        len(results), total_actions,
    )
    return {"orgs": len(results), "actions_created": total_actions}


@celery_app.task(name="app.tasks.client_success.execute_success_actions")
def execute_success_actions(limit: int = 200):
    """Hourly: send approved auto check-ins.

    "Approved" here means an active-agent org with ``auto_checkin_enabled`` — the
    daily scan only enqueues auto plays the org opted into, so a pending checkin
    is already approved-by-config. Escalations/celebrations/review_calls stay
    pending for a human to act on. We stamp ``executed_at`` and flip status to
    ``sent`` so the queue advances and nothing is double-processed.
    """
    logger.info("Executing approved Client Success check-ins")

    async def _run():
        async def _body(db):
            service = get_client_success_agent_service()
            result = await service.execute_pending_checkins(db, limit=limit)
            await db.commit()
            return result

        return await for_each_org(_body, label="execute_success_actions")

    results = _run_async(_run())
    total = sum((r or {}).get("sent", 0) for r in results if r)
    logger.info("Client Success check-ins executed: %s", total)
    return {"orgs": len(results), "sent": total}
