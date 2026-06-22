"""Client-management reports — retention, revenue pipeline, activity summary.

All three accept optional ``start_date`` / ``end_date`` query params (ISO 8601).
Rows are windowed and aggregated in Python so naive (SQLite) and aware
(Postgres) timestamps compare consistently.
"""

import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.client_health import ClientHealthSnapshot
from app.models.company import Company
from app.models.deal import Deal
from app.models.email_template import EmailSend
from app.models.interaction import Interaction
from app.models.task import Task
from app.schemas.company import VALID_STAGES
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


def _parse_bound(value: Optional[str]) -> Optional[datetime]:
    """Parse a ``start``/``end`` query value (ISO date or datetime) to an aware
    datetime, tolerating the ``YYYY-MM-DD`` shape the date pickers send. Returns
    ``None`` on empty/invalid input so a bad param never 500s the report."""
    if not value:
        return None
    try:
        return _aware(datetime.fromisoformat(value))
    except (ValueError, TypeError):
        return None


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


# ─── Frontend-facing report series ───────────────────────────────────────────
# The Reports page renders charts/tables that expect array (series) payloads.
# These endpoints accept ``start``/``end`` (ISO date) query params and return
# plain JSON arrays so the page's hooks (which coerce to arrays) light up.


@router.get("/revenue-funnel")
async def revenue_funnel(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Revenue + company count per lifecycle ``stage`` for the funnel view.

    ``value`` is the summed deal value of companies in that stage; ``count`` is
    how many companies sit in the stage. Every valid stage is present (zero
    filled) so the funnel renders a stable set of bars.
    """
    lo, hi = _parse_bound(start), _parse_bound(end)

    companies = list((await db.execute(select(Company.id, Company.stage))).all())
    stage_by_company = {cid: stage for cid, stage in companies}
    company_count: Counter = Counter(stage for _cid, stage in companies)

    deals = list((await db.execute(select(Deal))).scalars().all())
    value_by_stage: dict[str, float] = defaultdict(float)
    for d in deals:
        if not _in_window(d.created_at, lo, hi):
            continue
        stage = stage_by_company.get(d.company_id)
        if stage is not None:
            value_by_stage[stage] += _f(d.value)

    return [
        {
            "stage": stage,
            "value": round(value_by_stage.get(stage, 0.0), 2),
            "count": company_count.get(stage, 0),
        }
        for stage in VALID_STAGES
    ]


@router.get("/activity-heatmap")
async def activity_heatmap(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Daily activity counts (emails sent + interactions + tasks created) for the
    heatmap. Defaults to the last ~120 days when no window is given."""
    lo, hi = _parse_bound(start), _parse_bound(end)
    if lo is None:
        lo = datetime.now(timezone.utc) - timedelta(days=119)

    counts: dict[str, int] = defaultdict(int)

    def _bump(ts):
        ts = _aware(ts)
        if ts is None:
            return
        if lo is not None and ts < lo:
            return
        if hi is not None and ts > hi:
            return
        counts[ts.date().isoformat()] += 1

    for s in (await db.execute(select(EmailSend.sent_at))).scalars().all():
        _bump(s)
    for occurred in (await db.execute(select(Interaction.occurred_at))).scalars().all():
        _bump(occurred)
    for created in (await db.execute(select(Task.created_at))).scalars().all():
        _bump(created)

    return [{"date": day, "count": counts[day]} for day in sorted(counts)]


@router.get("/top-clients")
async def top_clients(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Companies ranked by won-deal revenue, for the Top Clients table.

    Honors the ``start``/``end`` window against each deal's ``close_date``.
    """
    lo, hi = _parse_bound(start), _parse_bound(end)

    rows = (
        await db.execute(
            select(
                Deal.company_id,
                Company.name,
                func.coalesce(func.sum(Deal.value), 0),
                func.count(Deal.id),
            )
            .join(Company, Company.id == Deal.company_id)
            .where(Deal.status == "won")
            .group_by(Deal.company_id, Company.name)
        )
    ).all()

    # Re-aggregate in Python when a window is given (close_date may be null and
    # the SQL above doesn't filter on it, to keep null-date won deals visible).
    if lo is not None or hi is not None:
        won = list((await db.execute(select(Deal).where(Deal.status == "won"))).scalars().all())
        names = {cid: name for cid, name, _v, _c in rows}
        revenue: dict = defaultdict(float)
        deal_count: dict = defaultdict(int)
        for d in won:
            if not _in_window(d.close_date, lo, hi):
                continue
            revenue[d.company_id] += _f(d.value)
            deal_count[d.company_id] += 1
        rows = [(cid, names.get(cid), revenue[cid], deal_count[cid]) for cid in revenue]

    ranked = sorted(rows, key=lambda r: _f(r[2]), reverse=True)[:limit]
    return [
        {
            "company_id": str(cid),
            "name": name,
            "revenue": round(_f(value), 2),
            "deals": int(count),
        }
        for cid, name, value, count in ranked
    ]


@router.get("/retention")
async def retention(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Monthly retention rate from recorded client-health snapshots.

    For each month, ``rate`` = share of snapshots that are not ``churned``.
    Returns an empty series when no snapshots exist yet (the page then shows its
    empty state rather than a misleading flat line)."""
    lo, hi = _parse_bound(start), _parse_bound(end)

    snapshots = list((await db.execute(select(ClientHealthSnapshot))).scalars().all())
    by_month_total: dict[str, int] = defaultdict(int)
    by_month_retained: dict[str, int] = defaultdict(int)
    for snap in snapshots:
        ts = _aware(snap.recorded_at)
        if ts is None or not _in_window(ts, lo, hi):
            continue
        key = f"{ts.year:04d}-{ts.month:02d}-01"
        by_month_total[key] += 1
        if snap.health_status != "churned":
            by_month_retained[key] += 1

    return [
        {
            "date": month,
            "rate": round(by_month_retained[month] / by_month_total[month] * 100, 1),
        }
        for month in sorted(by_month_total)
    ]
