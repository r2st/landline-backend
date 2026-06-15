"""Dashboard metrics and analytics endpoints."""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.company import Company
from app.models.contact import Contact
from app.models.lead_score import LeadScore
from app.models.outreach import OutreachEvent
from app.schemas.dashboard import (
    DashboardOverview,
    OutreachMetrics,
    PipelineStageStat,
    ScoringDistribution,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dashboard", tags=["dashboard"])


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

    # Pipeline stages
    result = await db.execute(
        select(Company.stage, func.count(Company.id))
        .group_by(Company.stage)
        .order_by(func.count(Company.id).desc())
    )
    pipeline = [
        PipelineStageStat(stage=row[0], count=row[1])
        for row in result.all()
    ]

    # Outreach metrics
    outreach = await _get_outreach_metrics(db)

    # Score distribution buckets
    score_dist = await _get_score_distribution(db)

    return DashboardOverview(
        total_companies=total_companies,
        total_contacts=total_contacts,
        total_scored=total_scored,
        avg_fit_score=avg_fit_score,
        pipeline=pipeline,
        outreach=outreach,
        score_distribution=score_dist,
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
