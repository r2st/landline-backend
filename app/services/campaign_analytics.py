"""Cross-campaign aggregation: one home for the campaign table's arithmetic.

Two surfaces read the same numbers — ``GET /dialtone/campaigns/analytics``
returns the whole table, and ``GET /dialtone/campaigns/dashboard`` returns the
handful of rows and alerts an operator should act on. They must agree. Computing
the aggregation twice is how a dashboard ends up naming a campaign as the best
performer that the table ranks fourth, and there is no way to tell from either
screen which one is lying.

So the six queries live here, and both routers call :func:`compute`.
"""

import logging
import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deal import Deal
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence
from app.schemas.sequence_analytics import (
    CampaignAnalytics,
    CampaignAnalyticsResponse,
    CampaignTrend,
)

logger = logging.getLogger(__name__)


def rate(numerator: int, denominator: int) -> float:
    """Fraction (0..1), 0 when the denominator is 0 (never divide by zero)."""
    return round(numerator / denominator, 4) if denominator else 0.0


# Sends below which a campaign's rates are not worth ranking on. A cold reply
# rate is a few percent, so a campaign needs low-hundreds of sends before one
# reply moves it less than a whole point; this is well under that and only
# claims to exclude the campaigns whose rates are pure noise.
CAMPAIGN_MIN_SENDS = 30

# Reply-rate movement (in absolute rate, so 0.005 = half a percentage point)
# below which a campaign counts as flat between two periods.
TREND_EPSILON = 0.005


def wilson_lower(successes: int, trials: int, z: float = 1.96) -> float:
    """Lower bound of the 95% Wilson score interval, as a fraction.

    Why this rather than the observed rate: a campaign with 1 reply in 12 sends
    has an observed reply rate of 8.3%, which beats every mature campaign in the
    table and is indistinguishable from luck. Ranking on the lower bound asks
    "what reply rate can this campaign defend?" — 12 sends defend almost
    nothing, 400 sends at 4% defend nearly 4% — so a campaign reaches the top of
    the list only once it has earned the position.

    Wilson rather than the textbook normal interval because the normal one is
    badly behaved exactly where cold outreach lives: small samples and rates near
    zero, where it happily returns a negative lower bound.
    """
    if trials <= 0:
        return 0.0
    phat = successes / trials
    denominator = 1 + z * z / trials
    centre = phat + z * z / (2 * trials)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * trials)) / trials)
    return round(max(0.0, (centre - margin) / denominator), 4)


def wilson_upper(successes: int, trials: int, z: float = 1.96) -> float:
    """Upper bound of the same 95% Wilson interval, as a fraction.

    The lower bound answers "how good can this campaign prove it is". The upper
    bound answers the question a dashboard needs before it tells someone to kill
    a campaign: "how good could it still turn out to be?" A campaign with 0
    replies in 20 sends looks like a failure and has an upper bound near 16% —
    it has not sent enough mail to have failed at anything. Calling a campaign a
    laggard only when its *upper* bound sits below the portfolio rate means the
    verdict is "this is worse", not "this is unlucky so far".
    """
    if trials <= 0:
        return 0.0
    phat = successes / trials
    denominator = 1 + z * z / trials
    centre = phat + z * z / (2 * trials)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * trials)) / trials)
    return round(min(1.0, (centre + margin) / denominator), 4)


async def compute(
    db: AsyncSession,
    *,
    days: Optional[int] = None,
    include_empty: bool = False,
    min_sent: int = 0,
    compare: bool = False,
) -> CampaignAnalyticsResponse:
    """Every campaign's headline numbers for the window, unsorted.

    Callers order the rows themselves — the table sorts by a user-supplied key,
    the dashboard ranks by the defensible reply rate — so imposing an order here
    would only be one both of them overwrite.

    ``compare`` adds each campaign's previous equal-length period; it requires
    ``days``, since there is no period before all time. Callers validate that
    and this quietly declines to compare rather than inventing a window.
    """
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days) if days else None
    previous_start = since - timedelta(days=days) if (compare and days and since) else None

    sequences = (
        await db.execute(select(OutreachSequence).order_by(OutreachSequence.name))
    ).scalars().all()
    if not sequences:
        return CampaignAnalyticsResponse(timeframe_days=days, campaigns=[], totals=None)

    # --- Event counts per sequence, one pass ---
    event_query = (
        select(
            OutreachEnrollment.sequence_id,
            OutreachEvent.event_type,
            func.count(OutreachEvent.id),
        )
        .join(OutreachEnrollment, OutreachEvent.enrollment_id == OutreachEnrollment.id)
        .group_by(OutreachEnrollment.sequence_id, OutreachEvent.event_type)
    )
    if since is not None:
        event_query = event_query.where(OutreachEvent.occurred_at >= since)
    counts: dict[uuid.UUID, dict[str, int]] = {}
    for sequence_id, event_type, n in (await db.execute(event_query)).all():
        counts.setdefault(sequence_id, {})[event_type] = n

    # --- The same counts for the period immediately before, when comparing ---
    previous: dict[uuid.UUID, dict[str, int]] = {}
    if previous_start is not None and since is not None:
        previous_query = (
            select(
                OutreachEnrollment.sequence_id,
                OutreachEvent.event_type,
                func.count(OutreachEvent.id),
            )
            .join(OutreachEnrollment, OutreachEvent.enrollment_id == OutreachEnrollment.id)
            # Half-open on the upper bound so an event exactly on the boundary is
            # counted once, in the current period. Two inclusive windows would
            # double-count it and the totals would not reconcile.
            .where(
                OutreachEvent.occurred_at >= previous_start,
                OutreachEvent.occurred_at < since,
            )
            .group_by(OutreachEnrollment.sequence_id, OutreachEvent.event_type)
        )
        for sequence_id, event_type, n in (await db.execute(previous_query)).all():
            previous.setdefault(sequence_id, {})[event_type] = n

    # --- Last send + AI-composition mix, per sequence ---
    # The composition markers live in the send event's metadata (there is no
    # EmailSend row on this path), so this reads the rows rather than aggregating
    # in SQL. Bounded by the timeframe and by sends actually made.
    sent_query = (
        select(
            OutreachEnrollment.sequence_id,
            OutreachEvent.occurred_at,
            OutreachEvent.event_metadata,
        )
        .join(OutreachEnrollment, OutreachEvent.enrollment_id == OutreachEnrollment.id)
        .where(OutreachEvent.event_type == "sent")
    )
    if since is not None:
        sent_query = sent_query.where(OutreachEvent.occurred_at >= since)

    ai_composed: dict[uuid.UUID, int] = {}
    depth_sum: dict[uuid.UUID, int] = {}
    depth_n: dict[uuid.UUID, int] = {}
    last_send: dict[uuid.UUID, datetime] = {}
    for sequence_id, occurred_at, meta in (await db.execute(sent_query)).all():
        meta = meta or {}
        if meta.get("ai_generated"):
            ai_composed[sequence_id] = ai_composed.get(sequence_id, 0) + 1
        personalization = meta.get("personalization") or {}
        if personalization:
            depth_sum[sequence_id] = depth_sum.get(sequence_id, 0) + int(
                personalization.get("depth", 0) or 0
            )
            depth_n[sequence_id] = depth_n.get(sequence_id, 0) + 1
        if occurred_at is not None:
            stamp = occurred_at if occurred_at.tzinfo else occurred_at.replace(tzinfo=timezone.utc)
            known = last_send.get(sequence_id)
            if known is None or stamp > known:
                last_send[sequence_id] = stamp

    # --- Enrollment counts + deal attribution, one pass each ---
    enrollment_rows = (
        await db.execute(
            select(
                OutreachEnrollment.sequence_id,
                OutreachEnrollment.status,
                func.count(OutreachEnrollment.id),
            ).group_by(OutreachEnrollment.sequence_id, OutreachEnrollment.status)
        )
    ).all()
    enrollments: dict[uuid.UUID, dict[str, int]] = {}
    for sequence_id, status, n in enrollment_rows:
        enrollments.setdefault(sequence_id, {})[status] = n

    deal_rows = (
        await db.execute(
            select(Deal.source_sequence_id, func.count(Deal.id))
            .where(Deal.source_sequence_id.isnot(None))
            .group_by(Deal.source_sequence_id)
        )
    ).all()
    deals = {sequence_id: n for sequence_id, n in deal_rows}

    campaigns: list[CampaignAnalytics] = []
    for sequence in sequences:
        c = counts.get(sequence.id, {})
        sent = c.get("sent", 0)
        if not sent and not include_empty:
            continue
        if sent < min_sent:
            continue
        by_status = enrollments.get(sequence.id, {})
        total_enrollments = sum(by_status.values())
        composed = ai_composed.get(sequence.id, 0)
        depth_count = depth_n.get(sequence.id, 0)

        trend = None
        if previous_start is not None and since is not None:
            p = previous.get(sequence.id, {})
            p_sent = p.get("sent", 0)
            p_replied = p.get("replied", 0)
            p_reply_rate = rate(p_replied, p_sent)
            p_bounce_rate = rate(p.get("bounced", 0), p_sent)
            reply_delta = round(rate(c.get("replied", 0), sent) - p_reply_rate, 4)

            # A direction needs both periods to carry enough volume to have a
            # rate worth differencing. Otherwise this is the classic dashboard
            # lie: 4 sends last week, 6 this week, "up 50%".
            if p_sent < CAMPAIGN_MIN_SENDS or sent < CAMPAIGN_MIN_SENDS:
                direction = "insufficient_data"
            elif reply_delta > TREND_EPSILON:
                direction = "improving"
            elif reply_delta < -TREND_EPSILON:
                direction = "declining"
            else:
                direction = "flat"

            trend = CampaignTrend(
                previous_start=previous_start,
                previous_end=since,
                previous_sent=p_sent,
                previous_replied=p_replied,
                previous_reply_rate=p_reply_rate,
                previous_bounce_rate=p_bounce_rate,
                sent_delta=sent - p_sent,
                reply_rate_delta=reply_delta,
                bounce_rate_delta=round(
                    rate(c.get("bounced", 0), sent) - p_bounce_rate, 4
                ),
                direction=direction,
                newly_active=p_sent == 0 and sent > 0,
            )

        campaigns.append(CampaignAnalytics(
            sequence_id=sequence.id,
            name=sequence.name,
            industry=sequence.industry,
            tone=getattr(sequence, "tone", None),
            status=sequence.status,
            is_active=bool(sequence.is_active),
            total_enrollments=total_enrollments,
            active_enrollments=by_status.get("active", 0),
            sent=sent,
            opened=c.get("opened", 0),
            clicked=c.get("clicked", 0),
            replied=c.get("replied", 0),
            bounced=c.get("bounced", 0),
            unsubscribed=c.get("unsubscribed", 0),
            open_rate=rate(c.get("opened", 0), sent),
            click_rate=rate(c.get("clicked", 0), sent),
            reply_rate=rate(c.get("replied", 0), sent),
            bounce_rate=rate(c.get("bounced", 0), sent),
            unsubscribe_rate=rate(c.get("unsubscribed", 0), sent),
            deals_attributed=deals.get(sequence.id, 0),
            conversion_rate=rate(deals.get(sequence.id, 0), total_enrollments),
            ai_composed=composed,
            ai_composed_rate=rate(composed, sent),
            avg_personalization_depth=(
                round(depth_sum.get(sequence.id, 0) / depth_count, 2) if depth_count else 0.0
            ),
            last_send_at=last_send.get(sequence.id),
            reply_rate_ci_low=wilson_lower(c.get("replied", 0), sent),
            sample_sufficient=sent >= CAMPAIGN_MIN_SENDS,
            trend=trend,
        ))

    totals = None
    if campaigns:
        t_sent = sum(c.sent for c in campaigns)
        t_enrolled = sum(c.total_enrollments for c in campaigns)
        t_opened = sum(c.opened for c in campaigns)
        t_clicked = sum(c.clicked for c in campaigns)
        t_replied = sum(c.replied for c in campaigns)
        t_bounced = sum(c.bounced for c in campaigns)
        t_unsub = sum(c.unsubscribed for c in campaigns)
        t_deals = sum(c.deals_attributed for c in campaigns)
        t_ai = sum(c.ai_composed for c in campaigns)
        # Depth is averaged over the campaigns that reported one, weighted by
        # their sends — an unweighted mean would let a 3-send campaign outvote a
        # 300-send one on the headline number.
        weighted = sum(c.avg_personalization_depth * c.sent for c in campaigns)
        totals = CampaignAnalytics(
            sequence_id=uuid.UUID(int=0),
            name="All campaigns",
            industry=None,
            tone=None,
            status="aggregate",
            is_active=True,
            total_enrollments=t_enrolled,
            active_enrollments=sum(c.active_enrollments for c in campaigns),
            sent=t_sent,
            opened=t_opened,
            clicked=t_clicked,
            replied=t_replied,
            bounced=t_bounced,
            unsubscribed=t_unsub,
            open_rate=rate(t_opened, t_sent),
            click_rate=rate(t_clicked, t_sent),
            reply_rate=rate(t_replied, t_sent),
            bounce_rate=rate(t_bounced, t_sent),
            unsubscribe_rate=rate(t_unsub, t_sent),
            deals_attributed=t_deals,
            conversion_rate=rate(t_deals, t_enrolled),
            ai_composed=t_ai,
            ai_composed_rate=rate(t_ai, t_sent),
            avg_personalization_depth=round(weighted / t_sent, 2) if t_sent else 0.0,
            last_send_at=max((c.last_send_at for c in campaigns if c.last_send_at), default=None),
            reply_rate_ci_low=wilson_lower(t_replied, t_sent),
            sample_sufficient=t_sent >= CAMPAIGN_MIN_SENDS,
        )

    # Named movers, filtered for sample size by ``direction`` already — a
    # dashboard's job is to say which two campaigns changed, not to hand back
    # thirty rows and a scroll bar. Biggest move first.
    movers = sorted(
        (c for c in campaigns if c.trend is not None),
        key=lambda c: abs(c.trend.reply_rate_delta),
        reverse=True,
    )
    return CampaignAnalyticsResponse(
        timeframe_days=days,
        campaigns=campaigns,
        totals=totals,
        improving=[c.name for c in movers if c.trend.direction == "improving"],
        declining=[c.name for c in movers if c.trend.direction == "declining"],
    )

