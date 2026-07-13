"""Buyer-intent signal API endpoints (feature 2.1).

Lists detected intent signals, exposes a per-company view, lets a rep trigger an
on-demand scan, and marks signals as actioned. Signals are company-level (not
user-scoped), matching the ``news`` router.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.company import Company
from app.models.intent_signal import IntentSignal
from app.services.intent_monitor import get_intent_monitor

logger = logging.getLogger(__name__)
router = APIRouter(tags=["intent"])


class IntentSignalRead(BaseModel):
    id: uuid.UUID
    company_id: uuid.UUID
    signal_type: str
    source: Optional[str] = None
    title: str
    description: Optional[str] = None
    url: Optional[str] = None
    relevance_score: int
    urgency_score: int
    combined_score: float
    buying_window_days: Optional[int] = None
    recommended_action: Optional[str] = None
    outreach_angle: Optional[str] = None
    detected_at: datetime
    expires_at: Optional[datetime] = None
    actioned: bool
    is_active: bool

    class Config:
        from_attributes = True


class IntentScanResponse(BaseModel):
    company_id: uuid.UUID
    signals_found: int
    signals: list[dict]


def _to_read(sig: IntentSignal, now: datetime) -> IntentSignalRead:
    return IntentSignalRead(
        id=sig.id,
        company_id=sig.company_id,
        signal_type=sig.signal_type,
        source=sig.source,
        title=sig.title,
        description=sig.description,
        url=sig.url,
        relevance_score=sig.relevance_score,
        urgency_score=sig.urgency_score,
        combined_score=sig.combined_score,
        buying_window_days=sig.buying_window_days,
        recommended_action=sig.recommended_action,
        outreach_angle=sig.outreach_angle,
        detected_at=sig.detected_at,
        expires_at=sig.expires_at,
        actioned=sig.actioned,
        is_active=sig.is_active(now),
    )


@router.get("/intent/signals", response_model=list[IntentSignalRead])
async def list_intent_signals(
    company_id: Optional[uuid.UUID] = Query(None),
    signal_type: Optional[str] = Query(None),
    active_only: bool = Query(True, description="Only signals still inside their buying window"),
    min_relevance: int = Query(0, ge=0, le=100),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """List intent signals, most relevant + recent first."""
    now = datetime.now(timezone.utc)
    query = select(IntentSignal)
    if company_id:
        query = query.where(IntentSignal.company_id == company_id)
    if signal_type:
        query = query.where(IntentSignal.signal_type == signal_type)
    if min_relevance:
        query = query.where(IntentSignal.relevance_score >= min_relevance)
    if active_only:
        query = query.where(
            or_(IntentSignal.expires_at.is_(None), IntentSignal.expires_at >= now)
        )
    query = query.order_by(
        desc(IntentSignal.relevance_score), desc(IntentSignal.detected_at)
    ).limit(limit)

    rows = (await db.execute(query)).scalars().all()
    return [_to_read(s, now) for s in rows]


@router.get("/intent/companies/{company_id}/signals", response_model=list[IntentSignalRead])
async def company_intent_signals(
    company_id: uuid.UUID,
    active_only: bool = Query(True),
    db: AsyncSession = Depends(get_db),
):
    """All intent signals for one company."""
    now = datetime.now(timezone.utc)
    query = select(IntentSignal).where(IntentSignal.company_id == company_id)
    if active_only:
        query = query.where(
            or_(IntentSignal.expires_at.is_(None), IntentSignal.expires_at >= now)
        )
    query = query.order_by(desc(IntentSignal.detected_at))
    rows = (await db.execute(query)).scalars().all()
    return [_to_read(s, now) for s in rows]


@router.post("/intent/scan/{company_id}", response_model=IntentScanResponse)
async def scan_company_intent(
    company_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Trigger an on-demand intent scan for a single company."""
    company = (
        await db.execute(select(Company).where(Company.id == company_id))
    ).scalar_one_or_none()
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")

    monitor = get_intent_monitor()
    signals = await monitor.scan_company(db, company)
    return IntentScanResponse(
        company_id=company_id, signals_found=len(signals), signals=signals
    )


@router.post("/intent/signals/{signal_id}/actioned", response_model=IntentSignalRead)
async def mark_signal_actioned(
    signal_id: uuid.UUID,
    actioned: bool = Query(True),
    db: AsyncSession = Depends(get_db),
):
    """Mark an intent signal as actioned (or un-action it)."""
    sig = (
        await db.execute(select(IntentSignal).where(IntentSignal.id == signal_id))
    ).scalar_one_or_none()
    if sig is None:
        raise HTTPException(status_code=404, detail="Intent signal not found")
    sig.actioned = actioned
    await db.flush()
    return _to_read(sig, datetime.now(timezone.utc))
