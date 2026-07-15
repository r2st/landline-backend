"""Client-management reports — retention, revenue pipeline, activity summary.

All three accept optional ``start_date`` / ``end_date`` query params (ISO 8601).
Rows are windowed and aggregated in Python so naive (SQLite) and aware
(Postgres) timestamps compare consistently.
"""

import logging
from collections import Counter, defaultdict
from datetime import date, datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.agent_daily_summary import AgentDailySummary
from app.models.client_health import ClientHealthSnapshot
from app.models.company import Company
from app.models.deal import Deal
from app.models.email_template import EmailSend
from app.models.interaction import Interaction
from app.models.lead_score import LeadScore
from app.models.prospecting import ProspectingRun
from app.models.task import Task
from app.services.daily_summary import generate_and_store_summary
from app.services.scoring import grade_for_fit
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


@router.get("/email-performance")
async def email_performance(
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
):
    """Email send stats grouped by day over the last N days.

    Returns per-day counts of total sends, opened, replied, and bounced.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    day_col = func.date(EmailSend.sent_at)
    result = await db.execute(
        select(
            day_col.label("day"),
            func.count(EmailSend.id).label("sent"),
            func.count(case((EmailSend.status == "opened", 1))).label("opened"),
            func.count(case((EmailSend.status == "replied", 1))).label("replied"),
            func.count(case((EmailSend.status == "bounced", 1))).label("bounced"),
        )
        .where(EmailSend.sent_at >= since)
        .group_by(day_col)
        .order_by(day_col)
    )
    return [
        {
            "date": str(r.day),
            "sent": r.sent,
            "opened": r.opened,
            "replied": r.replied,
            "bounced": r.bounced,
        }
        for r in result.all()
    ]


@router.get("/pipeline-analytics")
async def pipeline_analytics(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Pipeline conversion metrics: stage counts, conversion rates, deal cycle
    time, win/loss rates, and total pipeline value."""
    lo, hi = _parse_bound(start), _parse_bound(end)

    # --- Stage counts + deal value per stage ---
    companies = list(
        (await db.execute(select(Company.id, Company.stage))).all()
    )
    stage_company_ids: dict[str, list] = defaultdict(list)
    stage_counts: Counter = Counter()
    for cid, stage in companies:
        stage_counts[stage] += 1
        stage_company_ids[stage].append(cid)

    deals = list((await db.execute(select(Deal))).scalars().all())
    value_by_stage: dict[str, float] = defaultdict(float)
    for d in deals:
        if not _in_window(d.created_at, lo, hi):
            continue
        for stage, cids in stage_company_ids.items():
            if d.company_id in cids:
                value_by_stage[stage] += _f(d.value)
                break

    stages = [
        {
            "stage": stage,
            "count": stage_counts.get(stage, 0),
            "value": round(value_by_stage.get(stage, 0.0), 2),
        }
        for stage in VALID_STAGES
    ]

    # --- Sequential conversion rates ---
    conversion_rates = []
    stage_list = list(VALID_STAGES)
    for i in range(len(stage_list) - 1):
        from_stage = stage_list[i]
        to_stage = stage_list[i + 1]
        from_count = stage_counts.get(from_stage, 0)
        to_count = stage_counts.get(to_stage, 0)
        rate = round(to_count / from_count * 100, 1) if from_count > 0 else 0.0
        conversion_rates.append(
            {"from_stage": from_stage, "to_stage": to_stage, "rate": rate}
        )

    # --- Deal metrics ---
    won_deals = [d for d in deals if d.status == "won"]
    lost_deals = [d for d in deals if d.status == "lost"]
    pending_deals = [d for d in deals if d.status == "pending"]

    # Average deal cycle (won deals with a close_date)
    cycle_days = []
    for d in won_deals:
        if d.close_date and d.created_at:
            created = _aware(d.created_at)
            closed = _aware(d.close_date)
            if created and closed:
                delta = (closed - created).total_seconds() / 86400
                cycle_days.append(delta)
    avg_deal_cycle_days = round(sum(cycle_days) / len(cycle_days), 1) if cycle_days else 0.0

    # Win / loss rates
    total_decided = len(won_deals) + len(lost_deals) + len(pending_deals)
    win_rate = round(len(won_deals) / total_decided * 100, 1) if total_decided > 0 else 0.0
    loss_rate = round(len(lost_deals) / total_decided * 100, 1) if total_decided > 0 else 0.0

    # Total pipeline value (pending deals)
    total_pipeline_value = round(sum(_f(d.value) for d in pending_deals), 2)

    return {
        "stages": stages,
        "conversion_rates": conversion_rates,
        "avg_deal_cycle_days": avg_deal_cycle_days,
        "win_rate": win_rate,
        "loss_rate": loss_rate,
        "total_pipeline_value": total_pipeline_value,
    }


async def build_lead_quality_report(db: AsyncSession, days: int = 30) -> dict:
    """Assemble the lead-quality report used by the endpoint and weekly task.

    Grades every company by its latest fit score (A/B/C/D/F), counts qualified
    vs disqualified leads, breaks new leads down by acquisition source over the
    window, and attaches the most recent autonomous-prospecting run summary.
    """
    days = max(1, min(days, 365))
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=days)

    # Latest fit score per company (highest scored_at wins).
    scores = list((await db.execute(select(LeadScore))).scalars().all())
    latest_by_company: dict = {}
    for s in scores:
        cur = latest_by_company.get(s.company_id)
        if cur is None or _aware(s.scored_at) > _aware(cur.scored_at):
            latest_by_company[s.company_id] = s

    grade_counts: Counter = Counter()
    fit_values: list[int] = []
    for s in latest_by_company.values():
        grade_counts[grade_for_fit(s.fit_score)] += 1
        fit_values.append(s.fit_score)

    companies = list((await db.execute(select(Company))).scalars().all())
    # Unscored companies grade as F (not yet qualified).
    unscored = sum(1 for c in companies if c.id not in latest_by_company)
    grade_counts["F"] += unscored

    stage_counts: Counter = Counter(c.stage for c in companies)
    pipeline_counts: Counter = Counter(c.pipeline_stage for c in companies)

    new_by_source: Counter = Counter()
    new_leads = 0
    for c in companies:
        if _in_window(c.created_at, window_start, now):
            new_leads += 1
            new_by_source[c.source or "unknown"] += 1

    total_graded = sum(grade_counts.values())
    qualified = grade_counts["A"] + grade_counts["B"]
    disqualified = stage_counts.get("disqualified", 0)

    last_run = (
        await db.execute(
            select(ProspectingRun).order_by(ProspectingRun.started_at.desc()).limit(1)
        )
    ).scalar_one_or_none()
    prospecting = None
    if last_run is not None:
        prospecting = {
            "status": last_run.status,
            "started_at": last_run.started_at.isoformat() if last_run.started_at else None,
            "companies_found": last_run.companies_found,
            "companies_qualified": last_run.companies_qualified,
            "companies_enrolled": last_run.companies_enrolled,
            "companies_review_queue": last_run.companies_review_queue,
        }

    return {
        "window_days": days,
        "generated_at": now.isoformat(),
        "total_companies": len(companies),
        "grade_distribution": {g: grade_counts.get(g, 0) for g in ("A", "B", "C", "D", "F")},
        "qualified_leads": qualified,
        "qualified_rate": round(qualified / total_graded * 100, 1) if total_graded else 0.0,
        "disqualified_leads": disqualified,
        "average_fit_score": round(sum(fit_values) / len(fit_values), 1) if fit_values else 0.0,
        "new_leads_in_window": new_leads,
        "new_leads_by_source": dict(new_by_source),
        "stage_distribution": dict(stage_counts),
        "pipeline_distribution": dict(pipeline_counts),
        "last_prospecting_run": prospecting,
    }


@router.get("/lead-quality")
async def lead_quality(
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
):
    """Lead-quality report: A/B/C/D/F grade distribution, qualified vs
    disqualified counts, new-lead sources, average fit, and the latest
    autonomous-prospecting run summary."""
    return await build_lead_quality_report(db, days)


# --------------------------------------------------------------------------- #
# Daily autonomous-activity summary
# --------------------------------------------------------------------------- #


def _serialize_summary(row: AgentDailySummary) -> dict:
    """Shape an AgentDailySummary row for the API."""
    return {
        "id": str(row.id),
        "date": row.summary_date.isoformat(),
        "narrative": row.narrative,
        "emails_sent": row.emails_sent,
        "leads_discovered": row.leads_discovered,
        "replies_received": row.replies_received,
        "bounces_received": row.bounces_received,
        "follow_ups_created": row.follow_ups_created,
        "calls_queued": row.calls_queued,
        "metrics": row.metrics,
        "created_at": _aware(row.created_at),
        "updated_at": _aware(row.updated_at),
    }


async def _get_or_build_summary(
    db: AsyncSession, target_date, *, generate_if_missing: bool
) -> Optional[AgentDailySummary]:
    """Fetch a stored summary for ``target_date``; optionally compute-and-store it
    on the fly if it doesn't exist yet (never runs the AI pass on-demand)."""
    row = (
        await db.execute(
            select(AgentDailySummary).where(AgentDailySummary.summary_date == target_date)
        )
    ).scalar_one_or_none()
    if row is None and generate_if_missing:
        row = await generate_and_store_summary(
            db, target_date=target_date,
            tz_name=get_settings().daily_summary_timezone, use_ai=False,
        )
        await db.commit()
    return row


@router.get("/daily-summary")
async def daily_summary_latest(
    days: int = Query(1, ge=1, le=90, description="How many recent days of history to include"),
    db: AsyncSession = Depends(get_db),
):
    """Latest daily activity summary plus a short history window.

    Returns the most recent stored summary as ``latest`` and the last ``days``
    summaries (newest-first) as ``history`` — enough to drive the trend charts.
    If today's summary hasn't been generated yet, it is computed on the fly
    (without the AI narrative pass) so the card is never empty.
    """
    tz_name = get_settings().daily_summary_timezone
    try:
        from zoneinfo import ZoneInfo

        today = datetime.now(ZoneInfo(tz_name)).date()
    except Exception:
        today = datetime.now(timezone.utc).date()

    # Ensure today's summary exists so the card always has fresh data.
    await _get_or_build_summary(db, today, generate_if_missing=True)

    rows = list(
        (
            await db.execute(
                select(AgentDailySummary)
                .order_by(AgentDailySummary.summary_date.desc())
                .limit(days)
            )
        )
        .scalars()
        .all()
    )
    latest = rows[0] if rows else None
    return {
        "latest": _serialize_summary(latest) if latest else None,
        "history": [_serialize_summary(r) for r in rows],
    }


@router.get("/daily-summary/{summary_date}")
async def daily_summary_by_date(
    summary_date: str,
    db: AsyncSession = Depends(get_db),
):
    """The daily activity summary for a specific ``YYYY-MM-DD`` date.

    Computes and stores it on the fly if missing (no AI pass). Returns 400 for a
    malformed date and 404 only if the date is in the future (nothing to report).
    """
    try:
        target = date.fromisoformat(summary_date)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid date; use YYYY-MM-DD")

    tz_name = get_settings().daily_summary_timezone
    try:
        from zoneinfo import ZoneInfo

        today = datetime.now(ZoneInfo(tz_name)).date()
    except Exception:
        today = datetime.now(timezone.utc).date()
    if target > today:
        raise HTTPException(status_code=404, detail="No summary for a future date")

    row = await _get_or_build_summary(db, target, generate_if_missing=True)
    if row is None:
        raise HTTPException(status_code=404, detail="No summary for that date")
    return _serialize_summary(row)
