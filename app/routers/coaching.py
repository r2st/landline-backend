"""AI sales coach API endpoints (feature 2.4).

- ``GET /coaching/my-performance`` — the caller's latest performance snapshot,
  generated on demand if missing or stale.
- ``GET /coaching/recommendations`` — just the caller's ranked coaching items.
- ``GET /coaching/team`` — admin/manager only: every active rep's latest
  snapshot plus team benchmarks.
- ``POST /coaching/generate`` — (re)generate a snapshot for the caller, or for
  another user when the caller is an admin/manager.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import PRIVILEGED_ROLES, get_current_user, scope_users_to_org
from app.models.rep_performance import RepPerformance
from app.models.user import User
from app.services.sales_coach import (
    compute_team_benchmarks,
    generate_rep_performance,
    period_bounds,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["coaching"])

# Regenerate a snapshot rather than serve one older than this.
_STALE_AFTER = timedelta(hours=24)


class RepPerformanceRead(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    user_name: Optional[str] = None
    period_type: str
    period_start: datetime
    period_end: datetime
    metrics: dict
    ai_insights: dict
    coaching_recommendations: dict
    model_used: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


def _to_read(snap: RepPerformance, user_name: Optional[str] = None) -> RepPerformanceRead:
    return RepPerformanceRead(
        id=snap.id,
        user_id=snap.user_id,
        user_name=user_name,
        period_type=snap.period_type,
        period_start=snap.period_start,
        period_end=snap.period_end,
        metrics=snap.metrics or {},
        ai_insights=snap.ai_insights or {},
        coaching_recommendations=snap.coaching_recommendations or {},
        model_used=snap.model_used,
        created_at=snap.created_at,
    )


async def _latest_snapshot(
    db: AsyncSession, user_id: uuid.UUID, period_type: str
) -> Optional[RepPerformance]:
    return (
        await db.execute(
            select(RepPerformance)
            .where(RepPerformance.user_id == user_id)
            .where(RepPerformance.period_type == period_type)
            .order_by(desc(RepPerformance.period_start))
            .limit(1)
        )
    ).scalar_one_or_none()


async def _get_or_generate(
    db: AsyncSession, user: User, period_type: str, force: bool = False
) -> RepPerformance:
    """Return a fresh snapshot, generating one if missing/stale or forced."""
    snap = await _latest_snapshot(db, user.id, period_type)
    period_start, _ = period_bounds(period_type)
    now = datetime.now(timezone.utc)

    def _aware(dt):
        # SQLite (tests) returns naive datetimes; treat them as UTC.
        return dt if dt is None or dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

    fresh = (
        snap is not None
        and _aware(snap.period_start) >= period_start - timedelta(hours=12)
        and (now - _aware(snap.created_at)) < _STALE_AFTER
    )
    if snap is not None and fresh and not force:
        return snap
    return await generate_rep_performance(db, user, period_type=period_type)


@router.get("/coaching/my-performance", response_model=RepPerformanceRead)
async def my_performance(
    period_type: str = Query("weekly", pattern="^(weekly|monthly)$"),
    refresh: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The caller's own performance snapshot + AI coaching."""
    snap = await _get_or_generate(db, user, period_type, force=refresh)
    return _to_read(snap, user.name)


@router.get("/coaching/recommendations")
async def my_recommendations(
    period_type: str = Query("weekly", pattern="^(weekly|monthly)$"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Just the caller's ranked coaching recommendations."""
    snap = await _get_or_generate(db, user, period_type)
    recs = (snap.coaching_recommendations or {}).get("items", [])
    return {
        "user_id": str(user.id),
        "period_type": period_type,
        "recommendations": recs,
        "insights": snap.ai_insights or {},
    }


@router.get("/coaching/team")
async def team_performance(
    period_type: str = Query("weekly", pattern="^(weekly|monthly)$"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Team-wide performance + benchmarks. Admin/manager only."""
    if user.role not in PRIVILEGED_ROLES:
        raise HTTPException(
            status_code=403, detail="Team analytics require a manager or admin role."
        )

    period_start, period_end = period_bounds(period_type)
    benchmarks = await compute_team_benchmarks(db, period_start, period_end)

    # "The team" is the caller's org, not the platform. Unscoped, this listed
    # every active user of every customer — and then ran ``_get_or_generate``
    # over them, so it did not merely read the roster, it *wrote* a
    # RepPerformance row per stranger, stamped with the caller's org by the
    # insert hook. See ``scope_users_to_org``: User is outside the global filter.
    reps = (
        await db.execute(
            scope_users_to_org(select(User).where(User.is_active.is_(True)), user)
        )
    ).scalars().all()

    team = []
    for rep in reps:
        snap = await _get_or_generate(db, rep, period_type)
        team.append(_to_read(snap, rep.name))

    # Sort by reply rate desc so the leaderboard reads top-down.
    team.sort(key=lambda s: s.metrics.get("reply_rate", 0), reverse=True)
    return {
        "period_type": period_type,
        "benchmarks": benchmarks,
        "reps": team,
    }


@router.post("/coaching/generate", response_model=RepPerformanceRead)
async def generate_performance(
    target_user_id: Optional[uuid.UUID] = Query(None, description="Admins/managers only"),
    period_type: str = Query("weekly", pattern="^(weekly|monthly)$"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """(Re)generate a performance snapshot for the caller or (privileged) another rep."""
    target = user
    if target_user_id is not None and target_user_id != user.id:
        if user.role not in PRIVILEGED_ROLES:
            raise HTTPException(
                status_code=403, detail="Only managers/admins can generate coaching for others."
            )
        # Same shape as ``?view_as_user_id=``, and it had the same hole: a
        # manager could name any user id on the platform and get back a snapshot
        # labelled with another tenant's rep name. A foreign id is a 404 — the
        # caller learns nothing about whether it exists.
        target = (
            await db.execute(
                scope_users_to_org(select(User).where(User.id == target_user_id), user)
            )
        ).scalar_one_or_none()
        if target is None:
            raise HTTPException(status_code=404, detail="User not found")

    snap = await generate_rep_performance(db, target, period_type=period_type)
    return _to_read(snap, target.name)
