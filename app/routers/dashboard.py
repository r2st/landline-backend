"""Dashboard metrics and analytics endpoints."""

import logging
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, case, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services import cache
from app.models.company import Company
from app.models.contact import Contact
from app.models.deal import Deal
from app.models.lead_score import LeadScore
from app.models.outreach import OutreachEvent
from app.models.prospecting import ProspectCandidate
from app.models.task import Task
from app.models.email_template import EmailSend
from app.schemas.dashboard import (
    ActivityItem,
    DashboardKPIs,
    DashboardOverview,
    EmailTrendPoint,
    IndustryStat,
    OutreachMetrics,
    PipelineStageStat,
    ScoringDistribution,
    TopLead,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dashboard", tags=["dashboard"])

# Email sends that count as "delivered to the prospect" for the emails-sent metric.
SENT_STATUSES = ("sent", "skipped")


async def _count_emails_sent(
    db: AsyncSession,
    *,
    since: datetime | None = None,
    before: datetime | None = None,
) -> int:
    """How many emails went out, across BOTH tables that record a send.

    There is no single sent-emails table. Manual, template and reply sends write
    ``EmailSend``; the sequence engine writes ``OutreachEvent(event_type='sent')``
    and no ``EmailSend`` row at all — and that path carries the bulk of outreach
    volume. Every counter here read ``EmailSend`` alone.

    That was not merely a low number. The same dashboard takes ``emails_opened``
    from ``OutreachEvent``, so the opens were counted on the sequence side while
    the sends were not: an open rate built from the two tiles divides sequence
    opens by non-sequence sends, and can exceed 100%. A metric that is wrong in a
    direction that flatters the product is the expensive kind.

    Both halves take the same window, so the sum is a real count and not two
    different questions added together.
    """
    send_q = select(func.count(EmailSend.id)).where(
        EmailSend.status.in_(SENT_STATUSES)
    )
    event_q = select(func.count(OutreachEvent.id)).where(
        OutreachEvent.event_type == "sent"
    )
    if since is not None:
        send_q = send_q.where(EmailSend.sent_at >= since)
        event_q = event_q.where(OutreachEvent.occurred_at >= since)
    if before is not None:
        send_q = send_q.where(EmailSend.sent_at < before)
        event_q = event_q.where(OutreachEvent.occurred_at < before)

    sends = (await db.execute(send_q)).scalar() or 0
    events = (await db.execute(event_q)).scalar() or 0
    return sends + events


@router.get("/overview", response_model=DashboardOverview)
async def get_overview(db: AsyncSession = Depends(get_db)):
    """Get full dashboard overview with pipeline, outreach, and scoring stats.

    Short-TTL cached — this runs a dozen aggregate queries and is polled often."""
    return await cache.cached("dashboard:overview", lambda: _build_overview(db))


async def _build_overview(db: AsyncSession) -> DashboardOverview:
    # Total companies
    result = await db.execute(select(func.count(Company.id)))
    total_companies = result.scalar() or 0

    # Total contacts
    result = await db.execute(select(func.count(Contact.id)))
    total_contacts = result.scalar() or 0

    # Scored companies + average
    result = await db.execute(
        select(
            func.count(func.distinct(LeadScore.company_id)),
            func.avg(LeadScore.fit_score),
        ).where(LeadScore.score_type == "rule_based")
    )
    row = result.one()
    total_scored = row[0] or 0
    avg_fit_score = round(float(row[1]), 1) if row[1] else None

    # Emails actually sent, from both send tables — see _count_emails_sent.
    emails_sent = await _count_emails_sent(db)

    # Leads added in the last 7 days.
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    result = await db.execute(
        select(func.count(Company.id)).where(Company.created_at >= week_ago)
    )
    leads_this_week = result.scalar() or 0

    # Previous week window (7-14 days ago)
    two_weeks_ago = datetime.now(timezone.utc) - timedelta(days=14)

    # Leads added in the previous week (7-14 days ago)
    result = await db.execute(
        select(func.count(Company.id)).where(
            Company.created_at >= two_weeks_ago,
            Company.created_at < week_ago,
        )
    )
    leads_prev_week = result.scalar() or 0

    # Emails sent in the previous week (7-14 days ago)
    emails_sent_prev_week = await _count_emails_sent(
        db, since=two_weeks_ago, before=week_ago
    )

    # Contacts added in the previous week (7-14 days ago)
    result = await db.execute(
        select(func.count(Contact.id)).where(
            Contact.created_at >= two_weeks_ago,
            Contact.created_at < week_ago,
        )
    )
    contacts_prev_week = result.scalar() or 0

    # Deals won this month
    now = datetime.now(timezone.utc)
    first_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    result = await db.execute(
        select(
            func.count(Deal.id),
            func.coalesce(func.sum(Deal.value), 0),
        ).where(
            Deal.status == "won",
            Deal.created_at >= first_of_month,
        )
    )
    won_row = result.one()
    deals_won_this_month = won_row[0] or 0
    deals_won_value_this_month = float(won_row[1] or 0)

    # Emails sent today
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    emails_sent_today = await _count_emails_sent(db, since=today_start)

    # Emails opened (total from OutreachEvent)
    result = await db.execute(
        select(func.count(OutreachEvent.id)).where(
            OutreachEvent.event_type == "opened"
        )
    )
    emails_opened = result.scalar() or 0

    # Pipeline stages with deal values
    result = await db.execute(
        select(
            Company.stage,
            func.count(func.distinct(Company.id)),
            func.coalesce(func.sum(Deal.value), 0),
        )
        .outerjoin(Deal, and_(Deal.company_id == Company.id, Deal.status == "pending"))
        .group_by(Company.stage)
        .order_by(func.count(func.distinct(Company.id)).desc())
    )
    pipeline = [
        PipelineStageStat(stage=r[0], count=r[1], value=float(r[2]))
        for r in result.all()
    ]

    # Outreach metrics
    outreach = await _get_outreach_metrics(db)

    # Score distribution buckets
    score_dist = await _get_score_distribution(db)

    # Industry breakdown
    industry_breakdown = await _get_industry_breakdown(db)

    # Tasks due today (not completed/cancelled)
    result = await db.execute(
        select(func.count(Task.id)).where(
            Task.due_date.isnot(None),
            func.date(Task.due_date) == func.current_date(),
            Task.status.notin_(["completed", "cancelled"]),
        )
    )
    tasks_due_today = result.scalar() or 0

    # Active deals (pending)
    result = await db.execute(
        select(
            func.count(Deal.id),
            func.coalesce(func.sum(Deal.value), 0),
        ).where(Deal.status == "pending")
    )
    deals_row = result.one()
    active_deals_count = deals_row[0] or 0
    active_deals_value = float(deals_row[1] or 0)

    # Contacts added in the last 7 days
    result = await db.execute(
        select(func.count(Contact.id)).where(Contact.created_at >= week_ago)
    )
    contacts_this_week = result.scalar() or 0

    return DashboardOverview(
        total_companies=total_companies,
        total_contacts=total_contacts,
        total_scored=total_scored,
        avg_fit_score=avg_fit_score,
        emails_sent=emails_sent,
        leads_this_week=leads_this_week,
        leads_prev_week=leads_prev_week,
        tasks_due_today=tasks_due_today,
        active_deals_count=active_deals_count,
        active_deals_value=active_deals_value,
        contacts_this_week=contacts_this_week,
        contacts_prev_week=contacts_prev_week,
        emails_sent_prev_week=emails_sent_prev_week,
        deals_won_this_month=deals_won_this_month,
        deals_won_value_this_month=deals_won_value_this_month,
        emails_sent_today=emails_sent_today,
        emails_opened=emails_opened,
        pipeline=pipeline,
        outreach=outreach,
        score_distribution=score_dist,
        industry_breakdown=industry_breakdown,
    )


@router.get("/kpis", response_model=DashboardKPIs)
async def get_kpis(db: AsyncSession = Depends(get_db)):
    """Get quick KPI stats for the dashboard (short-TTL cached)."""
    return await cache.cached("dashboard:kpis", lambda: _build_kpis(db))


async def _build_kpis(db: AsyncSession) -> DashboardKPIs:
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)
    first_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # Emails sent today
    emails_sent_today = await _count_emails_sent(db, since=today_start)

    # Open rate from OutreachEvent (opened / sent * 100)
    result = await db.execute(
        select(
            OutreachEvent.event_type,
            func.count(OutreachEvent.id),
        ).where(
            OutreachEvent.event_type.in_(("sent", "opened"))
        ).group_by(OutreachEvent.event_type)
    )
    event_counts = {row[0]: row[1] for row in result.all()}
    total_sent_events = event_counts.get("sent", 0)
    total_opened_events = event_counts.get("opened", 0)
    open_rate = round(total_opened_events / max(total_sent_events, 1) * 100, 2)

    # Replies this week
    result = await db.execute(
        select(func.count(OutreachEvent.id)).where(
            OutreachEvent.event_type == "replied",
        )
    )
    # Filter by week if the model has a timestamp; since OutreachEvent may not
    # have a created_at, count all replies as a fallback.
    replies_this_week = result.scalar() or 0

    # Deals won this month
    result = await db.execute(
        select(
            func.count(Deal.id),
            func.coalesce(func.sum(Deal.value), 0),
        ).where(
            Deal.status == "won",
            Deal.created_at >= first_of_month,
        )
    )
    won_row = result.one()
    deals_won_this_month = won_row[0] or 0
    deals_won_value = float(won_row[1] or 0)

    # Active pipeline value (pending deals)
    result = await db.execute(
        select(func.coalesce(func.sum(Deal.value), 0)).where(
            Deal.status == "pending"
        )
    )
    active_pipeline_value = float(result.scalar() or 0)

    # Average response time is not directly available without reply timestamps
    # on OutreachEvent, so return None.
    avg_response_time_hours = None

    return DashboardKPIs(
        emails_sent_today=emails_sent_today,
        open_rate=open_rate,
        replies_this_week=replies_this_week,
        deals_won_this_month=deals_won_this_month,
        deals_won_value=deals_won_value,
        active_pipeline_value=active_pipeline_value,
        avg_response_time_hours=avg_response_time_hours,
    )


@router.get("/pipeline")
async def get_pipeline_stats(db: AsyncSession = Depends(get_db)):
    """Get pipeline stage breakdown (short-TTL cached)."""
    async def _build():
        result = await db.execute(
            select(Company.stage, func.count(Company.id))
            .group_by(Company.stage)
            .order_by(func.count(Company.id).desc())
        )
        return [{"stage": row[0], "count": row[1]} for row in result.all()]

    return await cache.cached("dashboard:pipeline", _build)


@router.get("/outreach-metrics", response_model=OutreachMetrics)
async def get_outreach_stats(db: AsyncSession = Depends(get_db)):
    """Get outreach email performance metrics (short-TTL cached)."""
    return await cache.cached("dashboard:outreach-metrics", lambda: _get_outreach_metrics(db))


@router.get("/industry", response_model=list[IndustryStat])
async def get_industry_breakdown(
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    """Get the company count by industry segment (top N, short-TTL cached)."""
    return await cache.cached(
        f"dashboard:industry:{limit}", lambda: _get_industry_breakdown(db, limit=limit)
    )


@router.get("/top-leads", response_model=list[TopLead])
async def get_top_leads(
    limit: int = Query(5, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    """The highest-scoring companies, by ICP fit where we have one.

    Ranked on the ICP fit score from the prospecting run that surfaced the
    company, falling back to the rule-based score. Rule-based alone hid every
    autonomously-sourced lead: Places/Apollo candidates are scored against the
    learned ICP and never get a `lead_scores` row, so they ranked NULL and
    `nullslast` parked them behind every hand-scored company — the leads the
    prospector just found were the ones you couldn't see.
    """
    latest_score = (
        select(
            LeadScore.company_id,
            func.max(LeadScore.scored_at).label("max_scored_at"),
        )
        .where(LeadScore.score_type == "rule_based")
        .group_by(LeadScore.company_id)
        .subquery()
    )
    # The newest candidate row per company: a company re-surfaced by a later run
    # is re-scored against the ICP as it stands now, and that verdict supersedes.
    latest_candidate = (
        select(
            ProspectCandidate.company_id,
            func.max(ProspectCandidate.created_at).label("max_created_at"),
        )
        .where(ProspectCandidate.company_id.isnot(None))
        .group_by(ProspectCandidate.company_id)
        .subquery()
    )

    effective_score = func.coalesce(ProspectCandidate.fit_score, LeadScore.fit_score)
    query = (
        select(
            Company.id,
            Company.name,
            Company.stage,
            Company.industry_segment,
            LeadScore.fit_score.label("rule_score"),
            ProspectCandidate.fit_score.label("icp_score"),
        )
        .outerjoin(latest_score, Company.id == latest_score.c.company_id)
        .outerjoin(
            LeadScore,
            and_(
                LeadScore.company_id == Company.id,
                LeadScore.scored_at == latest_score.c.max_scored_at,
                LeadScore.score_type == "rule_based",
            ),
        )
        .outerjoin(latest_candidate, Company.id == latest_candidate.c.company_id)
        .outerjoin(
            ProspectCandidate,
            and_(
                ProspectCandidate.company_id == Company.id,
                ProspectCandidate.created_at == latest_candidate.c.max_created_at,
            ),
        )
        .order_by(effective_score.desc().nullslast())
        .limit(limit)
    )
    result = await db.execute(query)
    return [
        TopLead(
            id=r.id,
            name=r.name,
            stage=r.stage,
            score=r.icp_score if r.icp_score is not None else r.rule_score,
            industry=r.industry_segment,
            score_type=(
                "icp"
                if r.icp_score is not None
                else ("rule_based" if r.rule_score is not None else None)
            ),
        )
        for r in result.all()
    ]


@router.get("/recent-activity", response_model=list[ActivityItem])
async def get_recent_activity(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Get a unified recent-activity feed across the data we track.

    Combines: newly added companies and contacts, lead scores, email
    sends, and deal events — sorted newest first.
    """
    activity: list[ActivityItem] = []

    # Recently added companies
    result = await db.execute(
        select(Company).order_by(Company.created_at.desc()).limit(limit)
    )
    for c in result.scalars().all():
        activity.append(ActivityItem(
            id=f"company-{c.id}",
            type="company_added",
            description=f"{c.name} added to pipeline",
            company_id=c.id,
            company_name=c.name,
            created_at=c.created_at,
        ))

    # Recently added contacts (join with Company for company_name)
    result = await db.execute(
        select(Contact, Company.name.label("co_name"))
        .outerjoin(Company, Company.id == Contact.company_id)
        .order_by(Contact.created_at.desc())
        .limit(limit)
    )
    for ct, co_name in result.all():
        name = ct.full_name
        label = f" ({ct.title})" if ct.title else ""
        activity.append(ActivityItem(
            id=f"contact-{ct.id}",
            type="contact_added",
            description=f"Added {name}{label}",
            company_id=ct.company_id,
            contact_id=ct.id,
            company_name=co_name,
            created_at=ct.created_at,
        ))

    # Recent lead scores
    result = await db.execute(
        select(LeadScore, Company.name)
        .outerjoin(Company, Company.id == LeadScore.company_id)
        .order_by(LeadScore.scored_at.desc())
        .limit(limit)
    )
    for score, company_name in result.all():
        activity.append(ActivityItem(
            id=f"score-{score.id}",
            type="lead_scored",
            description=f"{company_name or 'Company'} scored {score.fit_score}",
            company_id=score.company_id,
            company_name=company_name,
            created_at=score.scored_at,
        ))

    # Recent email sends
    result = await db.execute(
        select(EmailSend).order_by(EmailSend.sent_at.desc()).limit(limit)
    )
    for send in result.scalars().all():
        activity.append(ActivityItem(
            id=f"send-{send.id}",
            type="email_sent",
            description=f"Email sent to {send.to_email or 'a contact'}",
            contact_id=send.contact_id,
            created_at=send.sent_at,
        ))

    # Recently created deals
    result = await db.execute(
        select(Deal, Company.name)
        .outerjoin(Company, Company.id == Deal.company_id)
        .order_by(Deal.created_at.desc())
        .limit(limit)
    )
    for deal, company_name in result.all():
        activity.append(ActivityItem(
            id=f"deal-{deal.id}",
            type="deal_created",
            description=f"Deal created{' for ' + company_name if company_name else ''} — ${deal.value or 0:,.0f}",
            company_id=deal.company_id,
            company_name=company_name,
            created_at=deal.created_at,
        ))

    # Recently won deals
    result = await db.execute(
        select(Deal, Company.name)
        .outerjoin(Company, Company.id == Deal.company_id)
        .where(Deal.status == "won")
        .order_by(Deal.created_at.desc())
        .limit(limit)
    )
    for deal, company_name in result.all():
        activity.append(ActivityItem(
            id=f"deal-won-{deal.id}",
            type="deal_won",
            description=f"Deal won{' with ' + company_name if company_name else ''} — ${deal.value or 0:,.0f}",
            company_id=deal.company_id,
            company_name=company_name,
            created_at=deal.created_at,
        ))

    activity.sort(key=lambda a: _aware(a.created_at), reverse=True)
    return activity[:limit]


def _day_key(value) -> str:
    """Normalise whatever the driver returned for ``date(col)`` to ``YYYY-MM-DD``.

    Postgres hands back a ``date``, SQLite the string it stored — and SQLite's
    string carries the time for a ``TIMESTAMP`` column unless ``date()`` sees
    it, so the first ten characters are taken rather than trusted whole.
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)[:10]


@router.get("/email-trends", response_model=list[EmailTrendPoint])
async def get_email_trends(
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
):
    """Email volume per day, and what came back from it.

    This used to return ``sent`` and nothing else, from ``email_sends`` alone.
    Both halves of that were wrong for the thing consuming it.

    **It counted a fraction of the mail.** Sent mail lives in two tables (see
    CLAUDE.md): the sequence engine writes ``outreach_events`` and no
    ``EmailSend`` row at all, and the sequence engine is where the bulk of
    outreach volume goes. A dashboard trend built on ``email_sends`` alone plots
    manual and template sends and calls it the org's email volume.

    **It reported engagement it never returned.** The Analytics page's compare
    mode reads ``opened`` / ``replied`` off these points to show a
    period-over-period change; the fields did not exist, so every comparison
    resolved to 0 and the panel reported "no change" whatever had happened.
    Click rate was at least honest about it — a hardcoded 0 with a comment.

    Engagement is bucketed by **when it happened**, not by the send it belongs
    to, matching ``services.outreach_trends``. It is the conventional weekly
    open rate: slightly lagged against a send cohort, and the same convention on
    every surface here rather than two that disagree.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)

    # Half one: manual / template / reply sends, plus their engagement stamps.
    send_day = func.date(EmailSend.sent_at)
    manual = (
        await db.execute(
            select(
                send_day.label("day"),
                func.count(EmailSend.id),
                func.count(EmailSend.replied_at),
            )
            .where(EmailSend.sent_at >= since, EmailSend.status.in_(SENT_STATUSES))
            .group_by(send_day)
        )
    ).all()

    # ``opened_at`` / ``clicked_at`` are timestamps of the event, so they bucket
    # on their own date and need their own passes — a row sent on Monday and
    # opened on Wednesday belongs to both days, in different columns.
    async def _stamped(column):
        col_day = func.date(column)
        return (
            await db.execute(
                select(col_day.label("day"), func.count(EmailSend.id))
                .where(column.isnot(None), column >= since)
                .group_by(col_day)
            )
        ).all()

    opened_rows = await _stamped(EmailSend.opened_at)
    clicked_rows = await _stamped(EmailSend.clicked_at)

    # Half two: the sequence engine, where the outreach volume actually is.
    event_day = func.date(OutreachEvent.occurred_at)
    events = (
        await db.execute(
            select(event_day.label("day"), OutreachEvent.event_type, func.count(OutreachEvent.id))
            .where(
                OutreachEvent.occurred_at >= since,
                OutreachEvent.event_type.in_(("sent", "opened", "clicked", "replied", "bounced")),
            )
            .group_by(event_day, OutreachEvent.event_type)
        )
    ).all()

    buckets: dict[str, dict[str, int]] = {}

    def _bucket(raw_day) -> dict[str, int]:
        key = _day_key(raw_day)
        return buckets.setdefault(
            key, {"sent": 0, "opened": 0, "clicked": 0, "replied": 0, "bounced": 0}
        )

    for raw_day, sent, replied in manual:
        b = _bucket(raw_day)
        b["sent"] += sent
        b["replied"] += replied
    for raw_day, count in opened_rows:
        _bucket(raw_day)["opened"] += count
    for raw_day, count in clicked_rows:
        _bucket(raw_day)["clicked"] += count
    for raw_day, event_type, count in events:
        _bucket(raw_day)[event_type] += count

    return [
        EmailTrendPoint(date=day, **buckets[day])
        for day in sorted(k for k in buckets if k)
    ]


@router.get("/contact-growth")
async def contact_growth(
    days: int = Query(90, ge=7, le=365),
    db: AsyncSession = Depends(get_db),
):
    """New contacts added per week over the last N days."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    week_col = func.date_trunc('week', Contact.created_at)
    result = await db.execute(
        select(week_col.label("week"), func.count(Contact.id))
        .where(Contact.created_at >= since)
        .group_by(week_col)
        .order_by(week_col)
    )
    return [
        {"week": str(r[0].date() if hasattr(r[0], 'date') else r[0]), "count": r[1]}
        for r in result.all()
    ]


# --- Helpers ---

def _aware(dt: datetime) -> datetime:
    """Normalize a (possibly naive) datetime to aware UTC for safe sorting."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def _get_industry_breakdown(db: AsyncSession, limit: int = 10) -> list[IndustryStat]:
    """Company count grouped by industry segment (non-null), top N."""
    result = await db.execute(
        select(Company.industry_segment, func.count(Company.id))
        .where(Company.industry_segment.isnot(None))
        .group_by(Company.industry_segment)
        .order_by(func.count(Company.id).desc())
        .limit(limit)
    )
    return [IndustryStat(industry=r[0], count=r[1]) for r in result.all()]


async def _get_outreach_metrics(db: AsyncSession) -> OutreachMetrics:
    """Compute outreach metrics from event data."""
    result = await db.execute(
        select(
            OutreachEvent.event_type,
            func.count(OutreachEvent.id),
        )
        .group_by(OutreachEvent.event_type)
    )
    counts = {row[0]: row[1] for row in result.all()}

    total_sent = counts.get("sent", 0)
    total_delivered = counts.get("delivered", 0)
    total_opened = counts.get("opened", 0)
    total_clicked = counts.get("clicked", 0)
    total_replied = counts.get("replied", 0)
    total_bounced = counts.get("bounced", 0)

    # Rates based on sent count
    denominator = max(total_sent, 1)  # Avoid division by zero

    return OutreachMetrics(
        total_sent=total_sent,
        total_delivered=total_delivered,
        total_opened=total_opened,
        total_clicked=total_clicked,
        total_replied=total_replied,
        total_bounced=total_bounced,
        open_rate=round(total_opened / denominator * 100, 2),
        click_rate=round(total_clicked / denominator * 100, 2),
        reply_rate=round(total_replied / denominator * 100, 2),
        bounce_rate=round(total_bounced / denominator * 100, 2),
    )


async def _get_score_distribution(db: AsyncSession) -> list[ScoringDistribution]:
    """Get score distribution in buckets of 10."""
    result = await db.execute(
        select(
            case(
                (LeadScore.fit_score < 10, "0-9"),
                (LeadScore.fit_score < 20, "10-19"),
                (LeadScore.fit_score < 30, "20-29"),
                (LeadScore.fit_score < 40, "30-39"),
                (LeadScore.fit_score < 50, "40-49"),
                (LeadScore.fit_score < 60, "50-59"),
                (LeadScore.fit_score < 70, "60-69"),
                (LeadScore.fit_score < 80, "70-79"),
                (LeadScore.fit_score < 90, "80-89"),
                else_="90-100",
            ).label("bucket"),
            func.count(LeadScore.id),
        )
        .where(LeadScore.score_type == "rule_based")
        .group_by("bucket")
        .order_by("bucket")
    )
    return [
        ScoringDistribution(bucket=row[0], count=row[1])
        for row in result.all()
    ]
