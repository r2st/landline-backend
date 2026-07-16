"""Dashboard metrics and analytics endpoints."""

import logging
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, case, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
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


@router.get("/overview", response_model=DashboardOverview)
async def get_overview(db: AsyncSession = Depends(get_db)):
    """Get full dashboard overview with pipeline, outreach, and scoring stats."""
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

    # Emails actually composed/sent (from the compose flow's send log).
    result = await db.execute(
        select(func.count(EmailSend.id)).where(EmailSend.status.in_(SENT_STATUSES))
    )
    emails_sent = result.scalar() or 0

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
    result = await db.execute(
        select(func.count(EmailSend.id)).where(
            EmailSend.status.in_(SENT_STATUSES),
            EmailSend.sent_at >= two_weeks_ago,
            EmailSend.sent_at < week_ago,
        )
    )
    emails_sent_prev_week = result.scalar() or 0

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
    result = await db.execute(
        select(func.count(EmailSend.id)).where(
            EmailSend.status.in_(SENT_STATUSES),
            EmailSend.sent_at >= today_start,
        )
    )
    emails_sent_today = result.scalar() or 0

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
    """Get quick KPI stats for the dashboard."""
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)
    first_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # Emails sent today
    result = await db.execute(
        select(func.count(EmailSend.id)).where(
            EmailSend.status.in_(SENT_STATUSES),
            EmailSend.sent_at >= today_start,
        )
    )
    emails_sent_today = result.scalar() or 0

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
    """Get pipeline stage breakdown."""
    result = await db.execute(
        select(Company.stage, func.count(Company.id))
        .group_by(Company.stage)
        .order_by(func.count(Company.id).desc())
    )
    return [{"stage": row[0], "count": row[1]} for row in result.all()]


@router.get("/outreach-metrics", response_model=OutreachMetrics)
async def get_outreach_stats(db: AsyncSession = Depends(get_db)):
    """Get outreach email performance metrics."""
    return await _get_outreach_metrics(db)


@router.get("/industry", response_model=list[IndustryStat])
async def get_industry_breakdown(
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    """Get the company count by industry segment (top N)."""
    return await _get_industry_breakdown(db, limit=limit)


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


@router.get("/email-trends", response_model=list[EmailTrendPoint])
async def get_email_trends(
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
):
    """Get the number of emails sent per day over the last `days` days."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    day = func.date(EmailSend.sent_at)
    result = await db.execute(
        select(day.label("day"), func.count(EmailSend.id))
        .where(EmailSend.sent_at >= since, EmailSend.status.in_(SENT_STATUSES))
        .group_by(day)
        .order_by(day)
    )
    return [EmailTrendPoint(date=str(r[0]), sent=r[1]) for r in result.all()]


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
