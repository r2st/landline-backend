"""Daily lead-gen health check — the first consumer of the tracking API.

The tracking numbers (GET /leads/stats, commit 3603678) existed but nothing
polled them, so a dead pipeline could run silent for a week: the LLM chain
down (every send degrading to the static template), or the send path stalled
entirely, and every log line still saying "fine". This beat task reads the
same counters the API serves — ``lead_metrics.tally_by_org`` for sends, the
``ai_generated`` event-metadata flag (see ea828f6) for composition — and
shouts when today looks dead:

* ``sends_today == 0`` — no mail left on any path all day.
* ``ai_composed_rate == 0`` while sequence mail *did* go out — every send
  shipped as the static mail-merge fallback, i.e. AI composition is dead
  (out of quota, bad keys, provider chain dark) but delivery masks it.

Alerts are log lines at ERROR with a stable ``LEADGEN ALERT`` prefix so any
log-based alerting (CloudWatch, grep on the box) can key on them. The task
returns its numbers, so a manual trigger doubles as a health probe.

Runs platform-wide on purpose: no org context is bound in a beat task, so the
global org read filter is a no-op (see app.tasks.org_runner) and the totals
span every tenant — matching what a platform_admin sees on /leads/stats.
"""

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database_celery import celery_session
from app.models.outreach import OutreachEvent
from app.services import lead_metrics
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

ALERT_PREFIX = "LEADGEN ALERT"


async def collect_lead_gen_health(db: AsyncSession) -> dict:
    """Today's (UTC) send + AI-composition tallies, with alert strings.

    Pure read + compute, separated from the Celery wrapper so tests (and a
    shell) can run it against any session.
    """
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    tallies = await lead_metrics.tally_by_org(db, start, None)
    sends_today = sum(c.get("emails_sent", 0) for c in tallies.values())

    # AI-composed share of today's *sequence* sends. Same read-metadata-in-
    # Python approach as funnel_analytics: ``ai_generated`` lives in the event
    # JSON, not a column, and one day of sent events is small.
    seq_sends = 0
    ai_composed = 0
    rows = await db.execute(
        select(OutreachEvent.event_metadata).where(
            OutreachEvent.event_type == "sent",
            OutreachEvent.occurred_at >= start,
        )
    )
    for (metadata,) in rows.all():
        seq_sends += 1
        if isinstance(metadata, dict) and metadata.get("ai_generated"):
            ai_composed += 1
    ai_composed_rate = lead_metrics.rate(ai_composed, seq_sends)

    alerts: list[str] = []
    if sends_today == 0:
        alerts.append(
            f"{ALERT_PREFIX}: 0 emails sent today (as of {now.isoformat()}). "
            "The send pipeline may be stalled — check the outreach queue, "
            "sending accounts (sending_enabled), and the daily cap."
        )
    elif seq_sends > 0 and ai_composed == 0:
        # Only meaningful when sequence mail actually went out; with zero
        # sequence sends the rate is vacuous (and the case above already
        # fires when nothing at all was sent).
        alerts.append(
            f"{ALERT_PREFIX}: ai_composed_rate=0 across {seq_sends} sequence "
            "send(s) today — every send shipped as the static template. "
            "AI composition is likely dead: check GET /api/v1/ai/providers "
            "and provider keys/quota."
        )

    return {
        "date": start.date().isoformat(),
        "sends_today": sends_today,
        "sequence_sends_today": seq_sends,
        "ai_composed_today": ai_composed,
        "ai_composed_rate": ai_composed_rate,
        "alerts": alerts,
    }


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(name="app.tasks.lead_monitor.check_lead_gen_health")
def check_lead_gen_health():
    """Daily: poll the lead-gen tallies and log alerts when today looks dead."""

    async def _run():
        async with celery_session() as db:
            report = await collect_lead_gen_health(db)
        for alert in report["alerts"]:
            logger.error(alert)
        logger.info(
            "Lead-gen health %s: sends=%d, sequence_sends=%d, ai_composed=%d "
            "(rate=%.2f), alerts=%d",
            report["date"], report["sends_today"], report["sequence_sends_today"],
            report["ai_composed_today"], report["ai_composed_rate"],
            len(report["alerts"]),
        )
        return report

    return _run_async(_run())
