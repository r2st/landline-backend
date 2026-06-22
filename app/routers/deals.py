"""Deal CRUD and revenue-summary endpoints."""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.company import Company
from app.models.deal import Deal
from app.schemas.deal import (
    DealCreate,
    DealRead,
    DealStatusBreakdown,
    DealSummary,
    DealUpdate,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/deals", tags=["deals"])


def _f(value) -> float:
    """Coerce a (possibly Decimal/None) numeric to float."""
    return float(value) if value is not None else 0.0


@router.post("", response_model=DealRead, status_code=201)
async def create_deal(payload: DealCreate, db: AsyncSession = Depends(get_db)):
    """Create a deal for a company."""
    company = (
        await db.execute(select(Company.id).where(Company.id == payload.company_id))
    ).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    deal = Deal(**payload.model_dump())
    db.add(deal)
    await db.flush()
    await db.refresh(deal)
    logger.info("Created deal %s for company %s", deal.id, payload.company_id)
    return deal


@router.get("", response_model=list[DealRead])
async def list_deals(
    company_id: Optional[uuid.UUID] = Query(None),
    status: Optional[str] = Query(None, pattern="^(won|lost|pending)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """List deals, newest first, with optional filters."""
    query = select(Deal)
    if company_id:
        query = query.where(Deal.company_id == company_id)
    if status:
        query = query.where(Deal.status == status)
    query = query.order_by(Deal.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    return list((await db.execute(query)).scalars().all())


@router.get("/summary", response_model=DealSummary)
async def deals_summary(
    company_id: Optional[uuid.UUID] = Query(None, description="Scope to one company"),
    db: AsyncSession = Depends(get_db),
):
    """Revenue metrics across deals: won revenue, open pipeline, win rate."""
    base = select(Deal.status, func.count(Deal.id), func.coalesce(func.sum(Deal.value), 0))
    if company_id:
        base = base.where(Deal.company_id == company_id)
    base = base.group_by(Deal.status)

    rows = (await db.execute(base)).all()
    by_status_map = {status: (count, _f(total)) for status, count, total in rows}

    breakdown = [
        DealStatusBreakdown(status=s, count=by_status_map.get(s, (0, 0.0))[0],
                            total_value=by_status_map.get(s, (0, 0.0))[1])
        for s in ("won", "lost", "pending")
    ]

    won_count = by_status_map.get("won", (0, 0.0))[0]
    lost_count = by_status_map.get("lost", (0, 0.0))[0]
    pending_count = by_status_map.get("pending", (0, 0.0))[0]
    won_revenue = by_status_map.get("won", (0, 0.0))[1]
    lost_value = by_status_map.get("lost", (0, 0.0))[1]
    pipeline_value = by_status_map.get("pending", (0, 0.0))[1]
    decided = won_count + lost_count

    return DealSummary(
        total_deals=won_count + lost_count + pending_count,
        won_count=won_count,
        lost_count=lost_count,
        pending_count=pending_count,
        won_revenue=won_revenue,
        pipeline_value=pipeline_value,
        lost_value=lost_value,
        win_rate=round(won_count / decided * 100, 1) if decided else 0.0,
        by_status=breakdown,
    )


@router.get("/{deal_id}", response_model=DealRead)
async def get_deal(deal_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Get a single deal by ID."""
    deal = (await db.execute(select(Deal).where(Deal.id == deal_id))).scalar_one_or_none()
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")
    return deal


@router.patch("/{deal_id}", response_model=DealRead)
async def update_deal(
    deal_id: uuid.UUID, payload: DealUpdate, db: AsyncSession = Depends(get_db)
):
    """Update deal fields."""
    deal = (await db.execute(select(Deal).where(Deal.id == deal_id))).scalar_one_or_none()
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(deal, field, value)
    await db.flush()
    await db.refresh(deal)
    logger.info("Updated deal %s", deal_id)
    return deal


@router.delete("/{deal_id}", status_code=204)
async def delete_deal(deal_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Delete a deal."""
    deal = (await db.execute(select(Deal).where(Deal.id == deal_id))).scalar_one_or_none()
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")
    await db.delete(deal)
    await db.flush()
    logger.info("Deleted deal %s", deal_id)
