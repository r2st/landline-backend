"""Client health endpoint — GET /companies/{id}/health."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.health import ClientHealth
from app.services.health import compute_health_by_id

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
