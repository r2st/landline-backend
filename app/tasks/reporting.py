"""Celery task: generate Dialtone's daily autonomous-activity summary.

Runs once a day (6 PM IST via Celery Beat), aggregates everything Dialtone did
that day into ``agent_daily_summaries``, and optionally emails the digest to the
configured admin address.
"""

import asyncio
import logging

from app.config import get_settings
from app.tasks.org_runner import for_each_org
from app.services.daily_summary import (
    DEFAULT_REPORT_TZ,
    generate_and_store_summary,
)
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(name="app.tasks.reporting.generate_daily_summary")
def generate_daily_summary():
    """Build and persist today's Dialtone activity summary, then optionally email it."""
    logger.info("Generating Dialtone daily activity summary")
    settings = get_settings()

    async def _run():
        # Per-org: the daily activity summary is computed per tenant.
        async def _body(db):
            summary = await generate_and_store_summary(
                db,
                tz_name=getattr(settings, "daily_summary_timezone", DEFAULT_REPORT_TZ),
                use_ai=getattr(settings, "daily_summary_use_ai", True),
            )
            await db.commit()

            result = {
                "date": summary.summary_date.isoformat(),
                "emails_sent": summary.emails_sent,
                "leads_discovered": summary.leads_discovered,
                "replies_received": summary.replies_received,
                "bounces_received": summary.bounces_received,
            }

            # Optional: email the digest to the admin.
            if getattr(settings, "daily_summary_email_enabled", False):
                try:
                    from app.services.notifications import get_notification_service

                    notifier = get_notification_service()
                    subject = f"Dialtone daily activity — {summary.summary_date}"
                    body = (
                        f"{summary.narrative}\n\n"
                        f"Emails sent: {summary.emails_sent}\n"
                        f"Leads discovered: {summary.leads_discovered}\n"
                        f"Replies: {summary.replies_received}\n"
                        f"Bounces: {summary.bounces_received}\n"
                        f"Follow-ups: {summary.follow_ups_created}\n"
                        f"Calls queued: {summary.calls_queued}\n"
                    )
                    await notifier._send_notification(subject, body)
                    result["emailed"] = True
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("Daily summary email failed: %s", exc)
                    result["emailed"] = False

            logger.info("Daily summary generated: %s", result)
            return result

        return await for_each_org(_body, label="generate_daily_summary")

    try:
        return _run_async(_run())
    except Exception as exc:
        logger.error("generate_daily_summary failed: %s", str(exc))
        return {"status": "error", "error": str(exc)}
