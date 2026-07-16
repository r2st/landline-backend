"""Feature 2 — Auto-Pause on Danger Signals: health + resume endpoints.

Operator-facing surface for the deliverability safety rail. Shares the
``/dialtone`` prefix with the Console router but lives here to keep the
danger-signal state machine self-contained.

* ``GET  /dialtone/health/signals``        — current metrics + thresholds + status.
* ``GET  /dialtone/health/signals/events`` — trip history (paginated).
* ``POST /dialtone/resume``                — acknowledged manual resume.
* ``POST /dialtone/health/signals/test``   — dry-run the evaluator (no pause).

Resume is deliberately manual-only with acknowledgment — auto-pause is never
auto-resumed (that's the whole point of the rail), which also means no
pause/resume flapping.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.danger_signal import DangerSignalEvent
from app.models.user import User
from app.schemas.danger_signals import (
    ActiveTrip,
    DangerEvent,
    DangerEventList,
    DangerSignalsHealth,
    DangerTestResult,
    MetricStatus,
    ResumeRequest,
    ResumeResponse,
    ThresholdConfig,
)
from app.services import system_settings
from app.services.danger_monitor import DangerReport, MetricResult, get_danger_monitor

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dialtone", tags=["dialtone"])


def _metric_out(m: MetricResult) -> MetricStatus:
    return MetricStatus(
        metric=m.metric,
        scope=m.scope,
        window=m.window,
        observed_value=round(m.observed_value, 6),
        threshold=m.threshold,
        warn_threshold=m.warn_threshold,
        sample_size=m.sample_size,
        status=m.status,
        account_id=str(m.account_id) if m.account_id else None,
        account_email=m.account_email,
        hard_bounces=m.hard_bounces,
    )


def _thresholds_out(report: DangerReport) -> ThresholdConfig:
    t = report.thresholds
    return ThresholdConfig(
        bounce_rate=t.bounce_rate,
        bounce_rate_warn=t.bounce_rate_warn,
        complaint_rate=t.complaint_rate,
        complaint_rate_warn=t.complaint_rate_warn,
        hard_bounce_count=t.hard_bounce_count,
        hard_bounce_count_warn=t.hard_bounce_count_warn,
        min_sample_size=t.min_sample_size,
    )


def _event_out(e: DangerSignalEvent) -> DangerEvent:
    return DangerEvent(
        id=str(e.id),
        metric=e.metric,
        scope=e.scope,
        account_id=str(e.account_id) if e.account_id else None,
        account_email=e.account_email,
        window=e.window,
        observed_value=e.observed_value,
        threshold=e.threshold,
        sample_size=e.sample_size,
        action_taken=e.action_taken,
        details=e.details or {},
        triggered_at=e.triggered_at,
        resolved_at=e.resolved_at,
        resolved_by=e.resolved_by,
        resolve_note=e.resolve_note,
    )


async def _latest_unresolved_trip(db: AsyncSession) -> DangerSignalEvent | None:
    """The most recent auto-pause trip that hasn't been acknowledged/resolved."""
    row = await db.execute(
        select(DangerSignalEvent)
        .where(
            DangerSignalEvent.action_taken == "auto_paused",
            DangerSignalEvent.resolved_at.is_(None),
        )
        .order_by(DangerSignalEvent.triggered_at.desc())
        .limit(1)
    )
    return row.scalar_one_or_none()


@router.get("/health/signals", response_model=DangerSignalsHealth)
async def get_danger_signals(db: AsyncSession = Depends(get_db)):
    """Current danger metrics (global + per account), thresholds, and status."""
    report = await get_danger_monitor().evaluate(db)
    global_metrics = [m for m in report.metrics if m.scope == "global"]
    account_metrics = [m for m in report.metrics if m.scope == "account"]

    active_trip = None
    trip = await _latest_unresolved_trip(db)
    if trip is not None:
        active_trip = ActiveTrip(
            id=str(trip.id),
            metric=trip.metric,
            scope=trip.scope,
            account_email=trip.account_email,
            observed_value=trip.observed_value,
            threshold=trip.threshold,
            sample_size=trip.sample_size,
            reason=(trip.details or {}).get("reason"),
            triggered_at=trip.triggered_at,
        )

    return DangerSignalsHealth(
        generated_at=report.generated_at,
        window_hours=report.window_hours,
        overall_status=report.overall_status,
        sending_enabled=report.sending_enabled,
        breach=report.breach,
        thresholds=_thresholds_out(report),
        global_metrics=[_metric_out(m) for m in global_metrics],
        account_metrics=[_metric_out(m) for m in account_metrics],
        breaches=[_metric_out(m) for m in report.breaches],
        warnings=[_metric_out(m) for m in report.warnings],
        active_trip=active_trip,
    )


@router.get("/health/signals/events", response_model=DangerEventList)
async def get_danger_events(
    resolved: bool | None = Query(None, description="Filter by resolved/unresolved"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """History of danger-signal trips, newest-first, paginated."""
    filters = []
    if resolved is True:
        filters.append(DangerSignalEvent.resolved_at.isnot(None))
    elif resolved is False:
        filters.append(DangerSignalEvent.resolved_at.is_(None))

    count_q = select(func.count(DangerSignalEvent.id))
    for f in filters:
        count_q = count_q.where(f)
    total = (await db.execute(count_q)).scalar() or 0

    rows_q = select(DangerSignalEvent).order_by(DangerSignalEvent.triggered_at.desc())
    for f in filters:
        rows_q = rows_q.where(f)
    rows_q = rows_q.offset((page - 1) * page_size).limit(page_size)
    events = (await db.execute(rows_q)).scalars().all()

    return DangerEventList(
        items=[_event_out(e) for e in events],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/resume", response_model=ResumeResponse)
async def resume_sending(
    payload: ResumeRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Acknowledged manual resume after an auto-pause.

    Requires acknowledging the most recent unresolved trip. Re-checks live
    metrics and refuses (409) if the danger is still active unless ``force`` is
    passed (logged). On success, flips the kill switch back on and stamps every
    unresolved trip resolved.
    """
    trip = await _latest_unresolved_trip(db)
    if trip is None:
        raise HTTPException(
            status_code=409,
            detail="No active auto-pause to resume. Sending is not paused by a danger signal.",
        )
    if trip.id != payload.acknowledge_danger_signal_id:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Must acknowledge the most recent unresolved trip ({trip.id}), "
                f"not {payload.acknowledge_danger_signal_id}."
            ),
        )

    # Re-check live metrics — refuse to resume into a still-broken state.
    report = await get_danger_monitor().evaluate(db)
    still_breaching = report.breach
    if still_breaching and not payload.force:
        raise HTTPException(
            status_code=409,
            detail=(
                "Danger condition is still active — refusing to resume. "
                "Pass force=true (with a note) to override. Current breaches: "
                + "; ".join(
                    f"{m.metric}={m.observed_value} (>{m.threshold})" for m in report.breaches
                )
            ),
        )

    actor = f"operator:{user.email}"
    status = await system_settings.set_sending_enabled(
        db, enabled=True, updated_by=actor, reason=payload.note
    )

    # Resolve every currently-unresolved trip (resume re-enables globally).
    now = datetime.now(timezone.utc)
    unresolved = (
        await db.execute(
            select(DangerSignalEvent).where(
                DangerSignalEvent.action_taken == "auto_paused",
                DangerSignalEvent.resolved_at.is_(None),
            )
        )
    ).scalars().all()
    resolved_ids = []
    note = payload.note
    if still_breaching and payload.force:
        note = f"[FORCED override — still breaching] {payload.note}"
        logger.warning(
            "Dialtone resume FORCED by %s into a still-breaching state: %s", actor, payload.note
        )
    for e in unresolved:
        e.resolved_at = now
        e.resolved_by = actor
        e.resolve_note = note
        resolved_ids.append(str(e.id))

    await db.flush()
    logger.warning(
        "Dialtone RESUMED by %s (acknowledged trip %s, %d trip(s) resolved, forced=%s)",
        actor, trip.id, len(resolved_ids), bool(still_breaching and payload.force),
    )

    return ResumeResponse(
        resumed=True,
        sending_enabled=status.get("enabled", True),
        reason=payload.note,
        enrollments_affected=status.get("enrollments_affected", 0),
        resolved_event_ids=resolved_ids,
        forced=bool(still_breaching and payload.force),
        still_breaching=still_breaching,
    )


@router.post("/health/signals/test", response_model=DangerTestResult)
async def test_danger_signals(db: AsyncSession = Depends(get_db)):
    """Dry-run the evaluator: return what WOULD trip, without pausing anything."""
    report = await get_danger_monitor().evaluate(db)
    primary = report.primary_breach()
    return DangerTestResult(
        generated_at=report.generated_at,
        window_hours=report.window_hours,
        overall_status=report.overall_status,
        sending_enabled=report.sending_enabled,
        would_pause=report.breach and report.sending_enabled,
        would_pause_reason=(
            get_danger_monitor()._reason(primary)
            if (primary is not None and report.sending_enabled)
            else None
        ),
        breaches=[_metric_out(m) for m in report.breaches],
        warnings=[_metric_out(m) for m in report.warnings],
        metrics=[_metric_out(m) for m in report.metrics],
        thresholds=_thresholds_out(report),
    )
