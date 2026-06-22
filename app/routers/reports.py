"""Client-management reports — retention, revenue pipeline, activity summary.

All three accept optional ``start_date`` / ``end_date`` query params (ISO 8601).
Rows are windowed and aggregated in Python so naive (SQLite) and aware
(Postgres) timestamps compare consistently.
"""

import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.company import Company
from app.models.deal import Deal
from app.models.email_template import EmailSend
from app.models.interaction import Interaction
from app.models.task import Task
from app.schemas.reports import (
    ActivitySummaryReport,
    ClientRetentionReport,
    RevenuePipelineReport,
    ReportPeriod,
    StatusValue,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/reports", tags=["reports"])


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _in_window(ts: Optional[datetime], start: Optional[datetime], end: Optional[datetime]) -> bool:
    """True if ``ts`` falls within [start, end] (open-ended when a bound is None)."""
    ts = _aware(ts)
    if ts is None:
        return False
    if start is not None and ts < start:
        return False
    if end is not None and ts > end:
        return False
    return True


def _f(value) -> float:
    return float(value) if value is not None else 0.0


@router.get("/client-retention", response_model=ClientRetentionReport)
async def client_retention(
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Client retention: current client vs churned counts, plus deals won/lost
    in the period. Retention is computed from current pipeline stages (stage
    history isn't tracked)."""
    start, end = _aware(start_date), _aware(end_date)

    stage_rows = (
        await db.execute(
            select(Company.pipeline_stage, func.count(Company.id)).group_by(
                Company.pipeline_stage
            )
        )
    ).all()
    stage_counts = {stage: count for stage, count in stage_rows}
    total_clients = stage_counts.get("client", 0)
    total_churned = stage_counts.get("churned", 0)
    denom = total_clients + total_churned

    # Deals decided within the period (by close_date).
    deals = list((await db.execute(select(Deal))).scalars().all())
    won_count = won_rev = 0
    lost_count = lost_val = 0
    for d in deals:
        if not _in_window(d.close_date, start, end):
            continue
        if d.status == "won":
            won_count += 1
            won_rev += _f(d.value)
        elif d.status == "lost":
            lost_count += 1
            lost_val += _f(d.value)

    return ClientRetentionReport(
        period=ReportPeriod(start_date=start_date, end_date=end_date),
        total_clients=total_clients,
        total_churned=total_churned,
        retention_rate=round(total_clients / denom * 100, 1) if denom else 0.0,
        won_deals_in_period=won_count,
        won_revenue_in_period=won_rev,
        lost_deals_in_period=lost_count,
        lost_value_in_period=lost_val,
    )


@router.get("/revenue-pipeline", response_model=RevenuePipelineReport)
async def revenue_pipeline(
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Revenue + open-pipeline breakdown over deals created in the period."""
    start, end = _aware(start_date), _aware(end_date)

    deals = list((await db.execute(select(Deal))).scalars().all())
    in_period = [d for d in deals if _in_window(d.created_at, start, end)]

    counts: Counter = Counter()
    values: dict[str, float] = {"won": 0.0, "lost": 0.0, "pending": 0.0}
    for d in in_period:
        counts[d.status] += 1
        values[d.status] = values.get(d.status, 0.0) + _f(d.value)

    won_revenue = values.get("won", 0.0)
    pipeline_value = values.get("pending", 0.0)
    lost_value = values.get("lost", 0.0)
    won_count = counts.get("won", 0)

    by_status = [
        StatusValue(status=s, count=counts.get(s, 0), total_value=values.get(s, 0.0))
        for s in ("won", "lost", "pending")
    ]

    return RevenuePipelineReport(
        period=ReportPeriod(start_date=start_date, end_date=end_date),
        total_deals=len(in_period),
        won_revenue=won_revenue,
        pipeline_value=pipeline_value,
        lost_value=lost_value,
        average_won_deal_size=round(won_revenue / won_count, 2) if won_count else 0.0,
        by_status=by_status,
    )


@router.get("/activity-summary", response_model=ActivitySummaryReport)
async def activity_summary(
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Outreach + engagement activity counts over the period."""
    start, end = _aware(start_date), _aware(end_date)

    # Emails (sent in window) and replies (replied in window).
    sends = list((await db.execute(select(EmailSend))).scalars().all())
    emails_sent = sum(1 for s in sends if _in_window(s.sent_at, start, end))
    replies = sum(1 for s in sends if _in_window(s.replied_at, start, end))

    # Interactions by type (occurred in window).
    interactions = list((await db.execute(select(Interaction))).scalars().all())
    type_counts: Counter = Counter()
    for i in interactions:
        if _in_window(i.occurred_at, start, end):
            type_counts[i.interaction_type] += 1
    interactions_total = sum(type_counts.values())

    # Tasks created / completed in window.
    tasks = list((await db.execute(select(Task))).scalars().all())
    tasks_created = sum(1 for t in tasks if _in_window(t.created_at, start, end))
    tasks_completed = sum(
        1 for t in tasks
        if t.status == "completed" and _in_window(t.updated_at, start, end)
    )

    return ActivitySummaryReport(
        period=ReportPeriod(start_date=start_date, end_date=end_date),
        emails_sent=emails_sent,
        replies=replies,
        response_rate=round(replies / emails_sent, 3) if emails_sent else 0.0,
        interactions_total=interactions_total,
        interactions_by_type=dict(type_counts),
        meetings=type_counts.get("meeting", 0),
        tasks_created=tasks_created,
        tasks_completed=tasks_completed,
    )
