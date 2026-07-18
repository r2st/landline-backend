"""Celery tasks for the Social Listening Agent.

Two beat tasks, both fanned out per-org via :func:`for_each_org` so each tenant
is scanned under its own bound context (and one org's failure never aborts the
sweep):

* :func:`scan_signals` — the periodic fetch+analyze pass. Runs on the shortest
  configured cadence; each org's own ``scan_frequency_hours`` gates whether it's
  actually due this tick, so a daily-cadence org isn't scanned hourly.
* :func:`process_new_signals` — hourly escalation pass that turns already-stored
  high-relevance ``new`` signals into warm leads when the org has auto-create on
  (covers signals stored before a threshold/flag change).
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from app.services.social_listening_agent import get_social_listening_service
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


def _is_due(agent) -> bool:
    """Whether this org's agent is due for a scan given its own cadence.

    A never-scanned active agent is always due; otherwise it's due once
    ``scan_frequency_hours`` have elapsed since ``last_scan_at``.
    """
    if not agent.is_active:
        return False
    if agent.last_scan_at is None:
        return True
    last = agent.last_scan_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    due_after = last + timedelta(hours=max(1, agent.scan_frequency_hours or 24))
    return datetime.now(timezone.utc) >= due_after


@celery_app.task(name="app.tasks.social_listening.scan_signals")
def scan_signals():
    """Periodic per-org scan for public buying-intent signals (cadence-gated)."""
    logger.info("Starting social-listening signal scan")

    async def _run():
        service = get_social_listening_service()

        async def _body(db):
            agent = await service.get_or_create_agent(db)
            if not _is_due(agent):
                return {"scanned": False}
            result = await service.run_scan(db, agent)
            if result.get("signals_found"):
                logger.info(
                    "social-listening: org=%s found %d signals, %d leads",
                    agent.org_id, result.get("signals_found"), result.get("leads_created"),
                )
            return {"scanned": True, **result}

        return await for_each_org(_body, label="scan_signals")

    return _run_async(_run())


@celery_app.task(name="app.tasks.social_listening.process_new_signals")
def process_new_signals():
    """Hourly per-org pass: escalate stored high-relevance signals to leads."""
    logger.info("Starting social-listening new-signal processing")

    async def _run():
        service = get_social_listening_service()

        async def _body(db):
            return await service.process_new_signals(db)

        return await for_each_org(_body, label="process_new_signals")

    return _run_async(_run())
