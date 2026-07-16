"""Celery task for auto-pause on danger signals (Feature 2).

Runs every 5 minutes via Beat. Computes the 24h rolling deliverability metrics
and, if a threshold is breached AND sending is still enabled, engages the global
kill switch. Deliberately thin — all logic lives in
:mod:`app.services.danger_monitor`, mirroring the ``deliverability`` task /
``deliverability_optimizer`` split.

The task never raises: a monitoring bug must not crash the worker. It returns a
summary dict either way.
"""

import asyncio
import logging

from app.config import get_settings
from app.database_celery import celery_session
from app.services import system_settings
from app.services.danger_monitor import get_danger_monitor
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(name="app.tasks.danger_signals.monitor_danger_signals")
def monitor_danger_signals():
    """Evaluate 24h danger metrics; auto-pause on breach while sending is on."""

    async def _run():
        if not get_settings().danger_signals_enabled:
            return {"skipped": "danger_signals_disabled"}
        try:
            monitor = get_danger_monitor()
            async with celery_session() as db:
                report = await monitor.evaluate(db)
                tripped = False
                # Only trip when sending is still enabled — no point (and no
                # flapping) re-pausing an already-paused system.
                if report.breach and await system_settings.is_sending_enabled(db):
                    await monitor.trip(db, report)
                    tripped = True
                await db.commit()
                summary = report.summary()
                summary["tripped"] = tripped
                if tripped:
                    logger.error(
                        "monitor_danger_signals: AUTO-PAUSED (%d breach(es))",
                        len(report.breaches),
                    )
                elif report.warnings:
                    logger.warning(
                        "monitor_danger_signals: %d warning(s), no breach",
                        len(report.warnings),
                    )
                return summary
        except Exception as exc:  # never raise out of monitoring
            logger.exception("monitor_danger_signals failed")
            return {"error": str(exc), "tripped": False}

    return _run_async(_run())
