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
from app.models.deal_activity import DealActivity
from app.models.deal_note import DealNote
from app.schemas.deal import (
    DEAL_STAGE_PROBABILITIES,
    DealActivityRead,
    DealCreate,
    DealMetrics,
    DealNoteCreate,
    DealNoteRead,
    DealRead,
    DealStageBreakdown,
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

    activity = DealActivity(
        deal_id=deal.id,
        activity_type="created",
        description=f"Deal '{deal.title}' created",
        new_value=deal.status,
    )
    db.add(activity)
    await db.flush()

    logger.info("Created deal %s for company %s", deal.id, payload.company_id)
    return deal


@router.get("", response_model=list[DealRead])
async def list_deals(
    company_id: Optional[uuid.UUID] = Query(None),
    status: Optional[str] = Query(None, pattern="^(won|lost|pending)$"),
    deal_stage: Optional[str] = Query(None, pattern="^(qualification|discovery|proposal|negotiation|closing)$"),
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
    if deal_stage:
        query = query.where(Deal.deal_stage == deal_stage)
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


@router.get("/metrics", response_model=DealMetrics)
async def deal_metrics(
    company_id: Optional[uuid.UUID] = Query(None, description="Scope to one company"),
    db: AsyncSession = Depends(get_db),
):
    """Pipeline metrics: total/weighted value, avg deal size, win rate, days to close, by-stage breakdown."""
    # Pipeline value (pending deals)
    pipeline_q = select(
        func.coalesce(func.sum(Deal.value), 0),
        func.count(Deal.id),
    ).where(Deal.status == "pending")
    if company_id:
        pipeline_q = pipeline_q.where(Deal.company_id == company_id)
    row = (await db.execute(pipeline_q)).one()
    total_pipeline_value = _f(row[0])
    deal_count = row[1]
    average_deal_size = total_pipeline_value / deal_count if deal_count else 0.0

    # Weighted pipeline
    weighted_q = select(Deal.deal_stage, Deal.value).where(Deal.status == "pending")
    if company_id:
        weighted_q = weighted_q.where(Deal.company_id == company_id)
    deals_rows = (await db.execute(weighted_q)).all()
    weighted_pipeline_value = sum(
        _f(val) * DEAL_STAGE_PROBABILITIES.get(stage, 0) / 100
        for stage, val in deals_rows
    )

    # Win rate
    decided_q = select(Deal.status, func.count(Deal.id))
    if company_id:
        decided_q = decided_q.where(Deal.company_id == company_id)
    decided_q = decided_q.where(Deal.status.in_(["won", "lost"])).group_by(Deal.status)
    decided_rows = {status: cnt for status, cnt in (await db.execute(decided_q)).all()}
    won_count = decided_rows.get("won", 0)
    lost_count = decided_rows.get("lost", 0)
    decided = won_count + lost_count
    win_rate = round(won_count / decided * 100, 1) if decided else 0.0

    # Avg days to close (won deals)
    won_deals_q = select(Deal.created_at, Deal.close_date).where(Deal.status == "won", Deal.close_date.isnot(None))
    if company_id:
        won_deals_q = won_deals_q.where(Deal.company_id == company_id)
    won_deals = (await db.execute(won_deals_q)).all()
    if won_deals:
        total_days = sum((cd - ca).days for ca, cd in won_deals)
        avg_days_to_close = round(total_days / len(won_deals), 1)
    else:
        avg_days_to_close = 0.0

    # By-stage breakdown (pending deals)
    stage_q = select(
        Deal.deal_stage,
        func.count(Deal.id),
        func.coalesce(func.sum(Deal.value), 0),
    ).where(Deal.status == "pending")
    if company_id:
        stage_q = stage_q.where(Deal.company_id == company_id)
    stage_q = stage_q.group_by(Deal.deal_stage)
    stage_rows = (await db.execute(stage_q)).all()
    by_stage = [
        DealStageBreakdown(stage=stage, count=cnt, value=_f(val))
        for stage, cnt, val in stage_rows
    ]

    return DealMetrics(
        total_pipeline_value=total_pipeline_value,
        weighted_pipeline_value=round(weighted_pipeline_value, 2),
        average_deal_size=round(average_deal_size, 2),
        deal_count=deal_count,
        win_rate=win_rate,
        avg_days_to_close=avg_days_to_close,
        by_stage=by_stage,
    )


@router.get("/stages")
async def list_deal_stages():
    """Return the list of valid deal stages with probabilities."""
    return [
        {"stage": s, "probability": p, "label": s.replace("_", " ").title()}
        for s, p in DEAL_STAGE_PROBABILITIES.items()
    ]


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

    changes = payload.model_dump(exclude_unset=True)

    # Track stage change
    if "deal_stage" in changes and changes["deal_stage"] != deal.deal_stage:
        db.add(DealActivity(
            deal_id=deal.id,
            activity_type="stage_change",
            description=f"Stage changed from {deal.deal_stage} to {changes['deal_stage']}",
            old_value=deal.deal_stage,
            new_value=changes["deal_stage"],
        ))

    # Track status change
    if "status" in changes and changes["status"] != deal.status:
        db.add(DealActivity(
            deal_id=deal.id,
            activity_type="status_changed",
            description=f"Status changed from {deal.status} to {changes['status']}",
            old_value=deal.status,
            new_value=changes["status"],
        ))

    # Track value change
    if "value" in changes and changes["value"] != deal.value:
        db.add(DealActivity(
            deal_id=deal.id,
            activity_type="value_changed",
            description=f"Value changed from {deal.value} to {changes['value']}",
            old_value=str(deal.value) if deal.value is not None else None,
            new_value=str(changes["value"]) if changes["value"] is not None else None,
        ))

    for field, value in changes.items():
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


@router.post("/{deal_id}/notes", response_model=DealNoteRead, status_code=201)
async def add_deal_note(
    deal_id: uuid.UUID, payload: DealNoteCreate, db: AsyncSession = Depends(get_db)
):
    """Add a note to a deal."""
    deal = (await db.execute(select(Deal).where(Deal.id == deal_id))).scalar_one_or_none()
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")

    note = DealNote(deal_id=deal_id, content=payload.content)
    db.add(note)
    await db.flush()
    await db.refresh(note)

    db.add(DealActivity(
        deal_id=deal_id,
        activity_type="note_added",
        description="Note added to deal",
    ))
    await db.flush()

    logger.info("Added note %s to deal %s", note.id, deal_id)
    return note


@router.get("/{deal_id}/notes", response_model=list[DealNoteRead])
async def list_deal_notes(deal_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """List notes for a deal, newest first."""
    deal = (await db.execute(select(Deal).where(Deal.id == deal_id))).scalar_one_or_none()
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")
    query = (
        select(DealNote)
        .where(DealNote.deal_id == deal_id)
        .order_by(DealNote.created_at.desc())
    )
    return list((await db.execute(query)).scalars().all())


@router.get("/{deal_id}/activity", response_model=list[DealActivityRead])
async def list_deal_activity(deal_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Get activity timeline for a deal, newest first."""
    deal = (await db.execute(select(Deal).where(Deal.id == deal_id))).scalar_one_or_none()
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")
    query = (
        select(DealActivity)
        .where(DealActivity.deal_id == deal_id)
        .order_by(DealActivity.created_at.desc())
    )
    return list((await db.execute(query)).scalars().all())
