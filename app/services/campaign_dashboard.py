"""The campaign dashboard: which campaigns to act on, and what to do.

Every number here already existed somewhere. What did not exist was a screen
that ranks them and says so — so each consumer assembled its own from the
campaign table, the bounce summary and the enrollment queue, and each one
decided for itself what "worst campaign" meant. Three surfaces disagreeing about
which campaign is failing is worse than none, because there is no way to tell
from any of them which is right.

Two ideas do most of the work here.

**A rank needs an interval, not a rate.** The leaderboard orders campaigns by
the reply rate they can *defend* (the Wilson lower bound), so a campaign arrives
at the top by earning it rather than by being lucky twice. The laggard list is
the mirror image and uses the *upper* bound: a campaign is only called worse than
the portfolio when its entire 95% interval sits below it. Zero replies in twenty
sends has an upper bound near 16% — that campaign has not failed, it has barely
sent, and telling someone to kill it is the most expensive kind of wrong a
dashboard can be.

**An alert needs a number and a threshold.** Every alert carries the value that
raised it and the line it crossed, because an alert that says only "bounce rate
high" sends the reader to the table to find out how high, which is the work the
alert existed to save.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outreach import OutreachEnrollment
from app.schemas.campaign_dashboard import (
    CampaignDashboard,
    CampaignRank,
    DashboardAlert,
    DashboardWindow,
    PortfolioSummary,
)
from app.schemas.sequence_analytics import CampaignAnalytics
from app.services import campaign_analytics

logger = logging.getLogger(__name__)

DEFAULT_DAYS = 30
DEFAULT_LIST_SIZE = 5

# How overdue an enrollment must be before its campaign counts as stalled.
#
# A cadence gap is measured in days, so "no send today" says nothing. What is
# unambiguous is an enrollment the queue sweep believes is *due* and has not
# dispatched: the sweep runs every few minutes, so a due date a full day in the
# past is not a cadence, it is a campaign that has stopped — a paused mailbox, an
# exhausted daily cap, a send failure backing off, or a sender the danger monitor
# pulled. The dashboard cannot tell which, and does not guess; it says how many
# and how stale.
STALLED_OVERDUE_HOURS = 24

# Sends a campaign needs before zero replies is evidence of anything. At a 3%
# true reply rate, 90 sends produce no replies about 6% of the time — unusual
# enough to raise, common enough that raising it at 30 would mostly cry wolf.
NO_REPLY_MIN_SENDS = 3 * campaign_analytics.CAMPAIGN_MIN_SENDS

# AI-composed share below which a campaign is effectively shipping mail-merge
# while the composer is switched on. Not zero, because composition legitimately
# falls back per-send (no research brief, a rejected review, a provider blip);
# a quarter of sends personalized means the fallback has become the norm.
AI_COMPOSE_FLOOR = 0.25

# How far past the day's capacity demand must run before the backlog is called
# critical rather than a warning. At 1× the queue is rationing and today's mail
# still goes out; at 3× the arithmetic says the backlog cannot clear even if
# nothing new comes due, so it compounds every day until capacity changes.
CAPACITY_CRITICAL_MULTIPLE = 3.0

# Alert ordering within a severity. Reputation damage outranks lost opportunity,
# because a bounce rate spends something that cannot be bought back.
#
# ``capacity`` sits directly above ``stalled`` on purpose: a fleet-wide shortfall
# makes *every* campaign look stalled at once, and the stalled alert's advice
# ("check the mailbox, the daily cap, the quarantine") is four investigations
# the capacity alert has already finished. Ordered the other way, the reader
# works through a dozen symptoms before reaching the cause.
_KIND_ORDER = {
    "bounce_rate": 0,
    "capacity": 1,
    "stalled": 2,
    "ai_compose_dark": 3,
    "no_replies": 4,
    "declining": 5,
}
_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def _pct(fraction: float) -> str:
    """A rate as a percentage string for alert prose ('0.0642' → '6.4%')."""
    return f"{fraction * 100:.1f}%"


def _reason_leader(row: CampaignAnalytics) -> str:
    return (
        f"{row.replied} replies from {row.sent} sends — defends "
        f"{_pct(row.reply_rate_ci_low)} at 95% confidence"
    )


def _reason_laggard(row: CampaignAnalytics, ci_high: float, portfolio: float) -> str:
    return (
        f"{_pct(row.reply_rate)} over {row.sent} sends; even the optimistic end "
        f"of its range ({_pct(ci_high)}) is below the portfolio's "
        f"{_pct(portfolio)}"
    )


def _reason_thin(row: CampaignAnalytics) -> str:
    short = campaign_analytics.CAMPAIGN_MIN_SENDS - row.sent
    return (
        f"{row.sent} sends — {short} short of a readable rate; "
        f"currently anywhere from {_pct(row.reply_rate_ci_low)} to "
        f"{_pct(campaign_analytics.wilson_upper(row.replied, row.sent))}"
    )


def _rank(row: CampaignAnalytics, reason: str) -> CampaignRank:
    return CampaignRank(
        sequence_id=row.sequence_id,
        name=row.name,
        sent=row.sent,
        replied=row.replied,
        reply_rate=row.reply_rate,
        reply_rate_ci_low=row.reply_rate_ci_low,
        reply_rate_ci_high=campaign_analytics.wilson_upper(row.replied, row.sent),
        bounce_rate=row.bounce_rate,
        direction=row.trend.direction if row.trend else None,
        reason=reason,
    )


async def _overdue_by_sequence(
    db: AsyncSession, cutoff: datetime
) -> dict[uuid.UUID, int]:
    """Active enrollments whose due date passed before ``cutoff``, per campaign.

    Deliberately restricted to ``active``: a ``paused`` enrollment with a stale
    due date is the *correct* resting state for an out-of-office hold, a send
    quarantine or an operator's decision, and counting those as stalled would
    make every alert here permanent. Stalled means the queue thinks it should be
    sending this and it is not going out.
    """
    rows = (
        await db.execute(
            select(OutreachEnrollment.sequence_id, func.count(OutreachEnrollment.id))
            .where(
                OutreachEnrollment.status == "active",
                OutreachEnrollment.next_send_at.isnot(None),
                OutreachEnrollment.next_send_at < cutoff,
            )
            .group_by(OutreachEnrollment.sequence_id)
        )
    ).all()
    return {sequence_id: n for sequence_id, n in rows}


async def _due_now(db: AsyncSession, now: datetime) -> int:
    """Active enrollments the queue would dispatch this minute if it could.

    The demand side of the capacity question. Counted across every campaign,
    because the daily cap is an org-wide budget — a per-campaign count cannot
    say whether the fleet is over its limit, only that one campaign is waiting.
    """
    return (
        await db.execute(
            select(func.count(OutreachEnrollment.id)).where(
                OutreachEnrollment.status == "active",
                OutreachEnrollment.next_send_at.isnot(None),
                OutreachEnrollment.next_send_at <= now,
            )
        )
    ).scalar() or 0


def _capacity_alert(due: int, limit: int, sent_today: int) -> Optional[DashboardAlert]:
    """Demand against the day's send budget, when demand loses.

    This is the one condition on the dashboard that is nobody's fault and looks
    like everybody's. Every campaign with enrollments in the queue shows
    overdue sends; each raises its own ``stalled`` alert pointing at a mailbox,
    a cap and a quarantine; and the actual answer is that the org has more
    prospects due than it is allowed to email today. Reported once, at the
    portfolio level, with the arithmetic.

    Silent when capacity covers demand — a queue draining normally is not news,
    and a capacity line that renders every day is one nobody reads on the day it
    matters.
    """
    remaining = max(limit - sent_today, 0)
    if limit <= 0 or due <= remaining:
        return None

    unserved = due - remaining
    critical = due > limit * CAPACITY_CRITICAL_MULTIPLE
    return DashboardAlert(
        severity="critical" if critical else "warning",
        kind="capacity",
        message=(
            f"{due} enrollments are due now and {remaining} sends are left of "
            f"today's {limit} ({sent_today} already sent). {unserved} will not "
            f"go out today — the queue sends the highest-priority first and "
            f"holds the rest, so nothing is lost, but the backlog is "
            + (
                "growing faster than a day of capacity can clear it. Raise the "
                "daily limit, add a sending mailbox, or advance the warm-up plan; "
                "pausing enrollment is the fastest lever."
                if critical
                else "real. Any 'stalled' alerts below are this, not a broken "
                "campaign."
            )
        ),
        metric="due_enrollments",
        value=float(due),
        threshold=float(remaining),
    )


def _alerts(
    rows: list[CampaignAnalytics],
    overdue: dict[uuid.UUID, int],
    settings: Any,
    capacity: Optional[DashboardAlert] = None,
) -> list[DashboardAlert]:
    """Everything worth acting on, ranked by severity then by what it costs."""
    alerts: list[DashboardAlert] = []
    if capacity is not None:
        alerts.append(capacity)

    # Thresholds come from the danger monitor's configuration rather than a set
    # of the dashboard's own. The rail auto-pauses on a *blended* rate across
    # every campaign; a single campaign already past the same line is the thing
    # dragging the blend up, and it is invisible until the aggregate breaches —
    # by which point the reputation has already been spent.
    bounce_critical = float(getattr(settings, "danger_bounce_rate_threshold", 0.05))
    bounce_warn = float(getattr(settings, "danger_bounce_rate_warn", 0.03))
    min_sample = int(getattr(settings, "danger_min_sample_size", 20))
    ai_enabled = bool(getattr(settings, "ai_compose_enabled", False))

    for row in rows:
        if row.sent >= min_sample and row.bounce_rate >= bounce_warn:
            critical = row.bounce_rate >= bounce_critical
            alerts.append(DashboardAlert(
                severity="critical" if critical else "warning",
                kind="bounce_rate",
                sequence_id=row.sequence_id,
                campaign=row.name,
                message=(
                    f"{row.bounced} of {row.sent} sends bounced "
                    f"({_pct(row.bounce_rate)}). This campaign is on its own "
                    f"past the {'auto-pause' if critical else 'warning'} line "
                    f"the danger monitor applies to the whole fleet — the "
                    f"blended rate is being held down by the campaigns that "
                    f"are fine."
                ),
                metric="bounce_rate",
                value=row.bounce_rate,
                threshold=bounce_critical if critical else bounce_warn,
            ))

        stuck = overdue.get(row.sequence_id, 0)
        if stuck:
            alerts.append(DashboardAlert(
                severity="warning",
                kind="stalled",
                sequence_id=row.sequence_id,
                campaign=row.name,
                message=(
                    f"{stuck} active enrollments came due more than "
                    f"{STALLED_OVERDUE_HOURS}h ago and have not gone out. The "
                    f"queue believes it should be sending these — check the "
                    f"sending mailbox, the daily cap and the send-failure "
                    f"quarantine."
                ),
                metric="overdue_enrollments",
                value=float(stuck),
                threshold=float(STALLED_OVERDUE_HOURS),
            ))

        if ai_enabled and row.sent >= campaign_analytics.CAMPAIGN_MIN_SENDS \
                and row.ai_composed_rate < AI_COMPOSE_FLOOR:
            alerts.append(DashboardAlert(
                severity="warning",
                kind="ai_compose_dark",
                sequence_id=row.sequence_id,
                campaign=row.name,
                message=(
                    f"Only {_pct(row.ai_composed_rate)} of {row.sent} sends were "
                    f"AI-composed while composition is enabled — the rest went "
                    f"out as static mail-merge. The reply rate on this campaign "
                    f"is not measuring the copy you think it is. Check the "
                    f"provider chain and the compose review queue."
                ),
                metric="ai_composed_rate",
                value=row.ai_composed_rate,
                threshold=AI_COMPOSE_FLOOR,
            ))

        if row.sent >= NO_REPLY_MIN_SENDS and row.replied == 0:
            alerts.append(DashboardAlert(
                severity="warning",
                kind="no_replies",
                sequence_id=row.sequence_id,
                campaign=row.name,
                message=(
                    f"{row.sent} sends, no replies at all. At a normal cold "
                    f"reply rate that is unlikely enough to be worth reading "
                    f"the copy — or checking that replies are being matched "
                    f"back to this campaign."
                ),
                metric="replied",
                value=0.0,
                threshold=float(NO_REPLY_MIN_SENDS),
            ))

        if row.trend is not None and row.trend.direction == "declining":
            alerts.append(DashboardAlert(
                severity="info",
                kind="declining",
                sequence_id=row.sequence_id,
                campaign=row.name,
                message=(
                    f"Reply rate fell from {_pct(row.trend.previous_reply_rate)} "
                    f"to {_pct(row.reply_rate)} against the previous period, on "
                    f"comparable volume ({row.trend.previous_sent} → {row.sent} "
                    f"sends)."
                ),
                metric="reply_rate_delta",
                value=row.trend.reply_rate_delta,
                threshold=-campaign_analytics.TREND_EPSILON,
            ))

    alerts.sort(key=lambda a: (
        _SEVERITY_ORDER.get(a.severity, 9),
        _KIND_ORDER.get(a.kind, 9),
        a.campaign or "",
    ))
    return alerts


def _portfolio(rows: list[CampaignAnalytics], active_campaigns: int) -> PortfolioSummary:
    """Pool the window's sends across campaigns — never average the rates."""
    sent = sum(r.sent for r in rows)
    opened = sum(r.opened for r in rows)
    clicked = sum(r.clicked for r in rows)
    replied = sum(r.replied for r in rows)
    bounced = sum(r.bounced for r in rows)
    unsubscribed = sum(r.unsubscribed for r in rows)
    ai_composed = sum(r.ai_composed for r in rows)

    previous_sent = sum(r.trend.previous_sent for r in rows if r.trend)
    previous_replied = sum(r.trend.previous_replied for r in rows if r.trend)
    previous_reply_rate = campaign_analytics.rate(previous_replied, previous_sent)
    reply_rate = campaign_analytics.rate(replied, sent)
    delta = round(reply_rate - previous_reply_rate, 4)

    min_sends = campaign_analytics.CAMPAIGN_MIN_SENDS
    if previous_sent < min_sends or sent < min_sends:
        direction = "insufficient_data"
    elif delta > campaign_analytics.TREND_EPSILON:
        direction = "improving"
    elif delta < -campaign_analytics.TREND_EPSILON:
        direction = "declining"
    else:
        direction = "flat"

    # Depth is weighted by sends for the same reason the rates are pooled: a
    # 3-send campaign must not outvote a 300-send one on the headline number.
    weighted_depth = sum(r.avg_personalization_depth * r.sent for r in rows)

    return PortfolioSummary(
        campaigns=len(rows),
        active_campaigns=active_campaigns,
        sent=sent,
        opened=opened,
        clicked=clicked,
        replied=replied,
        bounced=bounced,
        unsubscribed=unsubscribed,
        open_rate=campaign_analytics.rate(opened, sent),
        click_rate=campaign_analytics.rate(clicked, sent),
        reply_rate=reply_rate,
        bounce_rate=campaign_analytics.rate(bounced, sent),
        unsubscribe_rate=campaign_analytics.rate(unsubscribed, sent),
        reply_rate_ci_low=campaign_analytics.wilson_lower(replied, sent),
        sample_sufficient=sent >= min_sends,
        ai_composed_rate=campaign_analytics.rate(ai_composed, sent),
        avg_personalization_depth=round(weighted_depth / sent, 2) if sent else 0.0,
        active_enrollments=sum(r.active_enrollments for r in rows),
        deals_attributed_lifetime=sum(r.deals_attributed for r in rows),
        previous_sent=previous_sent,
        previous_reply_rate=previous_reply_rate,
        reply_rate_delta=delta,
        direction=direction,
    )


async def build(
    db: AsyncSession,
    *,
    days: int = DEFAULT_DAYS,
    list_size: int = DEFAULT_LIST_SIZE,
    settings: Optional[Any] = None,
) -> CampaignDashboard:
    """Assemble the dashboard for the last ``days`` days."""
    if settings is None:
        from app.config import get_settings

        settings = get_settings()

    result = await campaign_analytics.compute(
        db, days=days, include_empty=True, compare=True
    )
    every_sequence = result.campaigns

    # Campaigns that sent in *either* window. A campaign that sent last period
    # and nothing this one belongs in the portfolio comparison — dropping it
    # would silently remove its previous volume from the denominator and report
    # a portfolio that improved by having a campaign stop.
    rows = [
        r for r in every_sequence
        if r.sent > 0 or (r.trend is not None and r.trend.previous_sent > 0)
    ]

    # Take the window from the aggregation rather than re-deriving it, so the
    # dashboard cannot report a window that is a few milliseconds off the one
    # the numbers were counted in.
    trend = next((r.trend for r in every_sequence if r.trend is not None), None)
    if trend is not None:
        previous_start, since = trend.previous_start, trend.previous_end
        until = since + timedelta(days=days)
    else:
        until = datetime.now(timezone.utc)
        since = until - timedelta(days=days)
        previous_start = since - timedelta(days=days)

    window = DashboardWindow(
        days=days,
        since=since,
        until=until,
        previous_start=previous_start,
        previous_end=since,
    )

    portfolio = _portfolio(rows, sum(1 for r in every_sequence if r.is_active))

    ranked = [r for r in rows if r.sample_sufficient]
    leaderboard = sorted(ranked, key=lambda r: r.reply_rate_ci_low, reverse=True)
    leaders = [_rank(r, _reason_leader(r)) for r in leaderboard[:list_size]]

    laggards: list[CampaignRank] = []
    for row in sorted(ranked, key=lambda r: r.reply_rate_ci_low):
        ci_high = campaign_analytics.wilson_upper(row.replied, row.sent)
        if ci_high < portfolio.reply_rate:
            laggards.append(_rank(row, _reason_laggard(row, ci_high, portfolio.reply_rate)))
        if len(laggards) >= list_size:
            break

    thin = sorted(
        (r for r in rows if r.sent > 0 and not r.sample_sufficient),
        key=lambda r: r.sent,
        reverse=True,
    )
    needs_volume = [_rank(r, _reason_thin(r)) for r in thin[:list_size]]

    now = datetime.now(timezone.utc)
    overdue = await _overdue_by_sequence(
        db, now - timedelta(hours=STALLED_OVERDUE_HOURS)
    )

    # Capacity is read live rather than from the analytics window: "how much
    # mail may we send today" is a fact about right now, and averaging it over
    # thirty days would report a limit that was never in force.
    #
    # Never lets a reporting call fail. A Redis blip or a missing warm-up plan
    # costs this one alert; it must not cost the dashboard.
    capacity: Optional[DashboardAlert] = None
    try:
        from app.services.send_limiter import get_send_limiter

        limit, sent_today = await get_send_limiter().daily_capacity(db, now=now)
        capacity = _capacity_alert(await _due_now(db, now), limit, sent_today)
    except Exception as exc:
        logger.warning("Capacity alert unavailable: %s", exc)

    # Alerts run over every sequence, not just the ones that sent: a campaign
    # whose enrollments are all stuck has no sends in the window *because* it is
    # broken, and filtering on sends would hide exactly the campaigns the alert
    # list exists for.
    alerts = _alerts(every_sequence, overdue, settings, capacity=capacity)

    return CampaignDashboard(
        window=window,
        portfolio=portfolio,
        leaderboard=leaders,
        laggards=laggards,
        needs_volume=needs_volume,
        alerts=alerts,
        campaign_count=sum(1 for r in rows if r.sent > 0),
    )
