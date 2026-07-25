"""Celery task for auto-pause on danger signals (Feature 2).

Runs every 5 minutes via Beat. Computes the 24h rolling deliverability metrics
and responds in proportion to what breached: a **global** breach engages the
system-wide kill switch (while sending is still enabled), whereas breaches
confined to **individual senders** pause just those mailboxes and leave the rest
of the fleet sending. Deliberately thin — all logic lives in
:mod:`app.services.danger_monitor`, mirroring the ``deliverability`` task /
``deliverability_optimizer`` split.

The task never raises: a monitoring bug must not crash the worker. It returns a
summary dict either way.
"""

import asyncio
import logging

from app.config import get_settings
from app.services import system_settings
from app.services.danger_monitor import get_danger_monitor
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


@celery_app.task(name="app.tasks.danger_signals.monitor_danger_signals")
def monitor_danger_signals():
    """Evaluate 24h danger metrics; auto-pause on breach while sending is on."""

    async def _run():
        if not get_settings().danger_signals_enabled:
            return {"skipped": "danger_signals_disabled"}
        try:
            monitor = get_danger_monitor()

            # Per-org: each organization's 24h metrics and kill switch are
            # independent — a spike in one tenant must not pause another.
            async def _body(db):
                report = await monitor.evaluate(db)
                tripped = False
                paused_accounts: list[dict] = []

                # Match the response to the scope of the breach. A global breach
                # is a shared-reputation problem and stops everything; a breach
                # confined to individual mailboxes pauses those mailboxes and
                # leaves the rest of the fleet sending.
                if report.global_breaches and await system_settings.is_sending_enabled(db):
                    # Only trip when sending is still enabled — no point (and no
                    # flapping) re-pausing an already-paused system.
                    await monitor.trip(db, report)
                    tripped = True
                elif report.account_breaches:
                    paused_accounts = await monitor.pause_accounts(db, report)

                await db.commit()
                summary = report.summary()
                summary["tripped"] = tripped
                summary["paused_accounts"] = paused_accounts
                if tripped:
                    logger.error(
                        "monitor_danger_signals: AUTO-PAUSED (%d breach(es))",
                        len(report.breaches),
                    )
                elif paused_accounts:
                    logger.error(
                        "monitor_danger_signals: paused %d sender(s): %s",
                        len(paused_accounts),
                        ", ".join(a["account_email"] or a["account_id"] for a in paused_accounts),
                    )
                elif report.warnings:
                    logger.warning(
                        "monitor_danger_signals: %d warning(s), no breach",
                        len(report.warnings),
                    )
                return summary

            return await for_each_org(_body, label="monitor_danger_signals")
        except Exception as exc:  # never raise out of monitoring
            logger.exception("monitor_danger_signals failed")
            return {"error": str(exc), "tripped": False}

    return _run_async(_run())
