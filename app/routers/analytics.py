"""Dialtone funnel analytics endpoint (P1 fix).

The single per-sequence / per-step reply-rate funnel, replacing the ad-hoc
``reply_rate`` / ``open_rate`` math scattered across the dashboard, A/B, coaching
and deliverability routers. All computation lives in
:mod:`app.services.funnel_analytics`; this router is a thin, org-scoped wrapper.
"""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.funnel_analytics import compute_funnel

router = APIRouter(prefix="/dialtone/analytics", tags=["dialtone-analytics"])


@router.get("/funnel")
async def funnel(
    sequence_id: Optional[UUID] = Query(
        None, description="Restrict the funnel to a single sequence."
    ),
    days: Optional[int] = Query(
        None, ge=1, le=365, description="Only count events in the trailing N days."
    ),
    db: AsyncSession = Depends(get_db),
):
    """Return the sent→delivered→opened→replied→positive funnel.

    Broken down per sequence and per step within each sequence, plus an org-wide
    total. Computed off ``OutreachEvent`` (the primary source of truth for
    outreach outcomes).
    """
    return await compute_funnel(db, sequence_id=sequence_id, days=days)
