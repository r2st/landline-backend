"""Outreach performance over time, rather than as one number.

Every analytics surface in Dialtone is a point-in-time aggregate. The console
says what happened today, ``/sequences/{id}/analytics`` says what a sequence has
done since it was created, ``/campaigns/analytics`` ranks them against each
other. All of it answers "what is our reply rate", and none of it answers the
question a dashboard exists for: **is it getting better or worse.**

That gap has a cost. Every change made to outreach — a new tone, AI composition,
a different cadence, warm-up finishing — is evaluated against a lifetime average
that mixes the weeks before the change into the weeks after and dilutes exactly
the signal you are looking for. A sequence sending since March with a 2.1% reply
rate might be at 4% this month and 1% last; the single number cannot tell you,
and it is the same number either way.

So this buckets the sequence engine's own events into periods and reports each
one, with the change against the period before it.

Scope
-----
Reads ``outreach_events`` only — the sequence engine — which is deliberate and
worth stating, because sent mail lives in two tables. ``email_sends`` holds
manual sends, template sends and inbox replies: real mail, but not the cold
outreach whose performance this measures, and folding it in would put replies to
conversations we were already having into a cold reply rate. Volume totals here
are therefore *not* the org's total email volume.

Partial periods
---------------
The newest bucket is nearly always incomplete — two days into a week — so its
*counts* are not comparable with a full period even though its *rates* are.
Every bucket says whether it is ``partial``. A trend chart that plots the
current week's half-height bar next to finished weeks reads as a collapse in
volume, which is the most common way a chart like this lies.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outreach import OutreachEnrollment, OutreachEvent

logger = logging.getLogger(__name__)

# Bucket widths, in days. Months are 30-day periods rather than calendar months
# on purpose: unequal buckets (28 vs 31 days) make volume look like it moved
# when only the calendar did, and this exists to show real movement.
INTERVAL_DAYS = {"day": 1, "week": 7, "month": 30}

# Event types that count toward the funnel. ``task_*`` (manual touches) are
# deliberately excluded from the email rates — a filed call reminder is not a
# send, and putting one in the denominator would make a multi-channel sequence
# look like it had a falling reply rate every time a human step ran.
_COUNTED = ("sent", "opened", "clicked", "replied", "bounced")

MAX_DAYS = 365


def _rate(numerator: int, denominator: int) -> float:
    """Percentage to one decimal, or 0.0 when there is nothing to divide."""
    if not denominator:
        return 0.0
    return round(100.0 * numerator / denominator, 1)


def _as_date(value: Any) -> Optional[date]:
    """Normalise whatever the driver returned for ``date(occurred_at)``.

    Postgres hands back a ``date``; SQLite hands back the string it stored. Both
    reach here, so both are handled rather than assuming the production driver.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        logger.warning("Unparseable event date %r — skipping", value)
        return None


async def timeline(
    db: AsyncSession,
    *,
    days: int = 90,
    interval: str = "week",
    sequence_id: Optional[UUID] = None,
) -> dict:
    """Bucketed outreach performance, oldest bucket first.

    Aggregated by day in SQL and rolled up to ``interval`` in Python. Grouping
    by day is portable across both databases in play; the roll-up is over at
    most ``days`` rows per event type, so doing it here costs nothing and buys a
    week/month boundary that does not depend on the database's week numbering.
    """
    interval = interval if interval in INTERVAL_DAYS else "week"
    width = INTERVAL_DAYS[interval]
    days = max(width, min(int(days), MAX_DAYS))

    now = datetime.now(timezone.utc)
    today = now.date()

    # Buckets are anchored to *today* and counted backwards, so the newest one
    # always ends today. Anchoring to a calendar boundary instead would make the
    # newest bucket a stub of whatever length the week happens to be so far, and
    # every boundary would shift under the caller from one day to the next.
    #
    # When ``days`` is not a whole number of periods the last bucket is still a
    # full one, so the window covers up to ``width - 1`` days more than asked.
    # Rounding the other way would drop real sends off the back of the window,
    # which is the worse error.
    starts = [
        today - timedelta(days=offset + width - 1)
        for offset in range(0, days, width)
    ]
    # Filter from the oldest bucket's own start, not from ``now - days``. Those
    # differ by a few hours, and events in the gap would be fetched, land in no
    # bucket, and vanish — a query that reads more than it reports.
    window_start = datetime.combine(
        min(starts), datetime.min.time(), tzinfo=timezone.utc
    )

    day_col = func.date(OutreachEvent.occurred_at)
    query = (
        select(day_col, OutreachEvent.event_type, func.count(OutreachEvent.id))
        .where(
            OutreachEvent.occurred_at >= window_start,
            OutreachEvent.event_type.in_(_COUNTED),
        )
        .group_by(day_col, OutreachEvent.event_type)
    )
    if sequence_id is not None:
        query = query.join(
            OutreachEnrollment, OutreachEvent.enrollment_id == OutreachEnrollment.id
        ).where(OutreachEnrollment.sequence_id == sequence_id)

    rows = (await db.execute(query)).all()

    # per_day[date][event_type] = n
    per_day: dict[date, dict[str, int]] = {}
    for raw_day, event_type, n in rows:
        day = _as_date(raw_day)
        if day is None:
            continue
        per_day.setdefault(day, {})[event_type] = n

    buckets: list[dict] = []
    for start in reversed(starts):
        end = start + timedelta(days=width - 1)
        totals = {key: 0 for key in _COUNTED}
        for offset in range(width):
            counts = per_day.get(start + timedelta(days=offset))
            if not counts:
                continue
            for key in _COUNTED:
                totals[key] += counts.get(key, 0)

        sent = totals["sent"]
        buckets.append({
            "period_start": start,
            "period_end": end,
            # The bucket containing today has not finished. Its rates are
            # comparable; its counts are not.
            "partial": end >= today,
            "sent": sent,
            "opened": totals["opened"],
            "clicked": totals["clicked"],
            "replied": totals["replied"],
            "bounced": totals["bounced"],
            "open_rate": _rate(totals["opened"], sent),
            "click_rate": _rate(totals["clicked"], sent),
            "reply_rate": _rate(totals["replied"], sent),
            "bounce_rate": _rate(totals["bounced"], sent),
        })

    # Change against the preceding bucket, in percentage *points* — the natural
    # unit for a difference between two rates. A relative change ("reply rate up
    # 50%") is unreadable at these magnitudes: 0.2% to 0.3% is a 50% rise and
    # also noise.
    for index, bucket in enumerate(buckets):
        previous = buckets[index - 1] if index else None
        bucket["reply_rate_change"] = (
            round(bucket["reply_rate"] - previous["reply_rate"], 1)
            if previous else None
        )
        bucket["open_rate_change"] = (
            round(bucket["open_rate"] - previous["open_rate"], 1)
            if previous else None
        )

    return {
        "interval": interval,
        "days": days,
        "buckets": buckets,
        **_summarize(buckets),
    }


def _summarize(buckets: list[dict]) -> dict:
    """Totals, and the direction of travel across the window.

    The direction compares the first half of the *complete* buckets against the
    second half rather than the newest bucket against the oldest. Two endpoints
    on a noisy series is not a trend — one quiet week at either end flips the
    answer — and the partial bucket is excluded because half a week of sends
    against a full week's is not a comparison.
    """
    complete = [b for b in buckets if not b["partial"]]

    totals = {
        key: sum(b[key] for b in buckets)
        for key in ("sent", "opened", "clicked", "replied", "bounced")
    }
    summary = {
        "total_sent": totals["sent"],
        "total_opened": totals["opened"],
        "total_clicked": totals["clicked"],
        "total_replied": totals["replied"],
        "total_bounced": totals["bounced"],
        "overall_open_rate": _rate(totals["opened"], totals["sent"]),
        "overall_click_rate": _rate(totals["clicked"], totals["sent"]),
        "overall_reply_rate": _rate(totals["replied"], totals["sent"]),
        "overall_bounce_rate": _rate(totals["bounced"], totals["sent"]),
        "reply_rate_trend": None,
        "direction": "insufficient_data",
    }

    # Fewer than four complete buckets cannot support a halves comparison worth
    # printing — with two, each "half" is one bucket, which is the endpoint
    # comparison this is trying not to be.
    if len(complete) < 4:
        return summary

    midpoint = len(complete) // 2
    older, newer = complete[:midpoint], complete[midpoint:]

    def _reply_rate(group: list[dict]) -> float:
        sent = sum(b["sent"] for b in group)
        return _rate(sum(b["replied"] for b in group), sent)

    # Pooled over each half rather than averaging the per-bucket rates: a week
    # with four sends and one reply would otherwise contribute a 25% reply rate
    # with the same weight as a week of four hundred.
    if sum(b["sent"] for b in older) == 0 or sum(b["sent"] for b in newer) == 0:
        return summary

    change = round(_reply_rate(newer) - _reply_rate(older), 1)
    summary["reply_rate_trend"] = change
    # A tenth of a point either way is not a direction. Cold reply rates are low
    # single digits, so the band is deliberately tight — but not zero, or every
    # window reports a trend.
    if change > 0.2:
        summary["direction"] = "improving"
    elif change < -0.2:
        summary["direction"] = "declining"
    else:
        summary["direction"] = "flat"
    return summary
