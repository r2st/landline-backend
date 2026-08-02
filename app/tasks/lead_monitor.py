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

**A sum is the wrong thing to alarm on, though.** Both conditions above were
evaluated against the platform total, so each one asked "is *every* tenant
dead?" — and answered "no" the moment a single healthy tenant existed. On a
one-tenant install that is the same question; from migration 078 onward it is
not, and the alarm quietly stopped covering anyone but the last org standing.
Note the shape: the reading was *correct*, the aggregation was the bug, and
nothing about a green health check tells you which question it answered.

Both failure modes are per-tenant in the first place. A stalled queue, a
mailbox with ``sending_enabled=False``, a daily cap already spent — all of it
is per-org state. AI composition especially: per-org LLM keys (migration 084)
mean one tenant's key running out of quota kills composition *for that tenant
only*, which is precisely the case a platform-wide rate cannot represent.

So the totals stay — they are what a platform_admin sees, and a genuinely
platform-wide outage should read as one alert rather than N — and the per-org
checks run underneath them:

* a platform-level alert **subsumes** the per-org ones of the same kind. An
  outage that took out everyone says so once.
* a per-org zero-send alert is gated on the org having at least one ``active``
  enrollment. An org with nothing enrolled sent nothing today because there was
  nothing to send, and a daily ERROR line per idle tenant is how the whole
  channel gets muted.
* the per-org AI check needs no gate — it is already conditioned on that org
  having sent sequence mail.
"""

import asyncio
import logging
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database_celery import celery_session
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.tenant import Organization
from app.services import lead_metrics
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

ALERT_PREFIX = "LEADGEN ALERT"


async def _orgs_with_active_enrollments(db: AsyncSession) -> set[uuid.UUID]:
    """Org ids holding at least one ``active`` enrollment.

    The gate on the per-org zero-send alert: this is what separates "the queue
    is stalled" from "this tenant has nothing enrolled". Only ``active``
    counts — a ``paused`` enrollment is deliberately not sending (kill switch,
    out-of-office, consent, an operator's decision), so counting it would alert
    on the system doing exactly what it was told.
    """
    rows = await db.execute(
        select(OutreachEnrollment.org_id)
        .where(OutreachEnrollment.status == "active")
        .distinct()
    )
    return {org_id for (org_id,) in rows.all() if org_id is not None}


async def collect_lead_gen_health(db: AsyncSession) -> dict:
    """Today's (UTC) send + AI-composition tallies, with alert strings.

    Pure read + compute, separated from the Celery wrapper so tests (and a
    shell) can run it against any session.

    Reads platform-wide, which holds because **no org context is bound** when
    the beat task calls it — the same precondition ``lead_metrics.tally_by_org``
    relies on. Called with an org bound, every read here narrows to that org
    together, so the report degrades to a single-tenant one rather than to a
    mix of scoped and unscoped numbers.
    """
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    tallies = await lead_metrics.tally_by_org(db, start, None)
    sends_by_org = {
        org_id: counter.get("emails_sent", 0) for org_id, counter in tallies.items()
    }
    sends_today = sum(sends_by_org.values())

    # AI-composed share of today's *sequence* sends. Same read-metadata-in-
    # Python approach as funnel_analytics: ``ai_generated`` lives in the event
    # JSON, not a column, and one day of sent events is small. Carries org_id so
    # the rate can be computed per tenant as well as across the platform — with
    # per-org LLM keys, one tenant's composition dying is invisible in the sum.
    seq_sends = 0
    ai_composed = 0
    seq_by_org: dict[Optional[uuid.UUID], list[int]] = defaultdict(lambda: [0, 0])
    rows = await db.execute(
        select(OutreachEvent.org_id, OutreachEvent.event_metadata).where(
            OutreachEvent.event_type == "sent",
            OutreachEvent.occurred_at >= start,
        )
    )
    for org_id, metadata in rows.all():
        seq_sends += 1
        seq_by_org[org_id][0] += 1
        if isinstance(metadata, dict) and metadata.get("ai_generated"):
            ai_composed += 1
            seq_by_org[org_id][1] += 1
    ai_composed_rate = lead_metrics.rate(ai_composed, seq_sends)

    org_names = {
        org_id: name
        for org_id, name in (
            await db.execute(
                select(Organization.id, Organization.name).where(
                    Organization.is_active.is_(True)
                )
            )
        ).all()
    }
    expecting_sends = await _orgs_with_active_enrollments(db)

    by_org = []
    for org_id, name in sorted(org_names.items(), key=lambda kv: kv[1]):
        org_seq, org_ai = seq_by_org.get(org_id, [0, 0])
        by_org.append(
            {
                "org_id": str(org_id),
                "org_name": name,
                "sends_today": sends_by_org.get(org_id, 0),
                "sequence_sends_today": org_seq,
                "ai_composed_today": org_ai,
                "ai_composed_rate": lead_metrics.rate(org_ai, org_seq),
                "expecting_sends": org_id in expecting_sends,
            }
        )

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

    # Per-org, for the far more common case of one tenant dying while the
    # platform total stays healthy. Skipped for whichever kind already fired
    # platform-wide: an outage that took out every tenant is one alert, not N.
    platform_stalled = sends_today == 0
    platform_ai_dead = seq_sends > 0 and ai_composed == 0
    for row in by_org:
        if (
            not platform_stalled
            and row["expecting_sends"]
            and row["sends_today"] == 0
        ):
            alerts.append(
                f"{ALERT_PREFIX}: org '{row['org_name']}' ({row['org_id']}) sent "
                f"0 emails today while the platform sent {sends_today} — this "
                "tenant's pipeline looks stalled. Check its outreach queue, its "
                "sending accounts (sending_enabled), its kill switch and its "
                "daily cap."
            )
        elif (
            not platform_ai_dead
            and row["sequence_sends_today"] > 0
            and row["ai_composed_today"] == 0
        ):
            alerts.append(
                f"{ALERT_PREFIX}: org '{row['org_name']}' ({row['org_id']}) shipped "
                f"all {row['sequence_sends_today']} of today's sequence send(s) as "
                "the static template. AI composition is dead for this tenant — "
                "check its own LLM key (per-org keys, migration 084) before the "
                "shared provider chain."
            )

    return {
        "date": start.date().isoformat(),
        "sends_today": sends_today,
        "sequence_sends_today": seq_sends,
        "ai_composed_today": ai_composed,
        "ai_composed_rate": ai_composed_rate,
        "by_org": by_org,
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
            "(rate=%.2f), orgs=%d, alerts=%d",
            report["date"], report["sends_today"], report["sequence_sends_today"],
            report["ai_composed_today"], report["ai_composed_rate"],
            len(report["by_org"]), len(report["alerts"]),
        )
        return report

    return _run_async(_run())
