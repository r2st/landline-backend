"""Client health endpoint — GET /companies/{id}/health."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.health import (
    ClientHealth,
    ClientHealthSnapshotRead,
    HealthSnapshotCreate,
)
from app.services.health import (
    compute_health_by_id,
    get_health_history,
    record_health_snapshot,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/companies", tags=["health"])


@router.get("/{company_id}/health", response_model=ClientHealth)
async def company_health(company_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Compute a client-health score (0-100) and status for a company.

    Health is derived from activity signals: recency of last contact, email
    response rate, and meeting frequency over the last 90 days.
    """
    health = await compute_health_by_id(db, company_id)
    if health is None:
        raise HTTPException(status_code=404, detail="Company not found")
    return health


@router.post(
    "/{company_id}/health/snapshot",
    response_model=ClientHealthSnapshotRead,
    status_code=201,
)
async def create_health_snapshot(
    company_id: uuid.UUID,
    payload: HealthSnapshotCreate | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Record a point-in-time health snapshot so engagement can be tracked
    over time. Recomputes health from current activity and persists it,
    optionally with a manual ``satisfaction_score``.
    """
    satisfaction = payload.satisfaction_score if payload is not None else None
    snapshot = await record_health_snapshot(db, company_id, satisfaction)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Company not found")
    return snapshot


@router.get(
    "/{company_id}/health/history",
    response_model=list[ClientHealthSnapshotRead],
)
async def company_health_history(
    company_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """Return a company's recorded health snapshots, most recent first."""
    return await get_health_history(db, company_id, limit)
