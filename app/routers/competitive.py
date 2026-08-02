"""Competitive intelligence API endpoints (feature 2.6).

CRUD for tracked competitors, AI battle-card generation, competitor-mention
listing/detection, and a win/loss correlation view. Competitors are global
(not user-scoped), matching the knowledge base.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.competitor import Competitor, CompetitorMention
from app.models.deal import Deal
from app.services.competitive_intelligence import get_competitive_intelligence

logger = logging.getLogger(__name__)
router = APIRouter(tags=["competitive"])


# --- Schemas ---

class CompetitorCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    domain: Optional[str] = None
    aliases: list[str] = Field(default_factory=list)
    products: list[str] = Field(default_factory=list)
    notes: Optional[str] = None
    generate_battle_card: bool = False


class CompetitorUpdate(BaseModel):
    name: Optional[str] = None
    domain: Optional[str] = None
    aliases: Optional[list[str]] = None
    products: Optional[list[str]] = None
    strengths: Optional[list[str]] = None
    weaknesses: Optional[list[str]] = None
    battle_card: Optional[dict] = None
    notes: Optional[str] = None
    is_active: Optional[bool] = None


class CompetitorRead(BaseModel):
    id: uuid.UUID
    name: str
    domain: Optional[str] = None
    aliases: list = []
    products: list = []
    strengths: list = []
    weaknesses: list = []
    battle_card: dict = {}
    notes: Optional[str] = None
    is_active: bool
    last_updated: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class CompetitorMentionRead(BaseModel):
    id: uuid.UUID
    competitor_id: uuid.UUID
    competitor_name: Optional[str] = None
    company_id: Optional[uuid.UUID] = None
    contact_id: Optional[uuid.UUID] = None
    deal_id: Optional[uuid.UUID] = None
    source: str
    context: Optional[str] = None
    sentiment: Optional[str] = None
    detected_at: datetime

    class Config:
        from_attributes = True


class DetectRequest(BaseModel):
    text: str = Field(..., min_length=1)
    source: str = "email"
    company_id: Optional[uuid.UUID] = None
    contact_id: Optional[uuid.UUID] = None
    deal_id: Optional[uuid.UUID] = None
    persist: bool = True


# --- Competitor CRUD ---

@router.get("/competitive/competitors", response_model=list[CompetitorRead])
async def list_competitors(
    active_only: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    query = select(Competitor).order_by(Competitor.name)
    if active_only:
        query = query.where(Competitor.is_active.is_(True))
    return list((await db.execute(query)).scalars().all())


@router.post("/competitive/competitors", response_model=CompetitorRead, status_code=201)
async def create_competitor(
    payload: CompetitorCreate,
    db: AsyncSession = Depends(get_db),
):
    # Existence check. This function is the only thing enforcing name
    # uniqueness — the schema has no constraint — so a duplicate that got past
    # it must still produce the 409, not a 500.
    existing = (
        await db.execute(
            select(Competitor.id)
            .where(func.lower(Competitor.name) == payload.name.lower())
            .limit(1)
        )
    ).scalars().first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="A competitor with that name already exists")

    competitor = Competitor(
        name=payload.name,
        domain=payload.domain,
        aliases=payload.aliases,
        products=payload.products,
        notes=payload.notes,
    )
    db.add(competitor)
    await db.flush()

    if payload.generate_battle_card:
        ci = get_competitive_intelligence()
        await ci.refresh_battle_card(db, competitor)

    await db.refresh(competitor)
    return competitor


@router.get("/competitive/competitors/{competitor_id}", response_model=CompetitorRead)
async def get_competitor(
    competitor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    competitor = (
        await db.execute(select(Competitor).where(Competitor.id == competitor_id))
    ).scalar_one_or_none()
    if competitor is None:
        raise HTTPException(status_code=404, detail="Competitor not found")
    return competitor


@router.patch("/competitive/competitors/{competitor_id}", response_model=CompetitorRead)
async def update_competitor(
    competitor_id: uuid.UUID,
    payload: CompetitorUpdate,
    db: AsyncSession = Depends(get_db),
):
    competitor = (
        await db.execute(select(Competitor).where(Competitor.id == competitor_id))
    ).scalar_one_or_none()
    if competitor is None:
        raise HTTPException(status_code=404, detail="Competitor not found")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(competitor, field, value)
    competitor.last_updated = datetime.now(timezone.utc)
    await db.flush()
    await db.refresh(competitor)
    return competitor


@router.delete("/competitive/competitors/{competitor_id}", status_code=204)
async def delete_competitor(
    competitor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    competitor = (
        await db.execute(select(Competitor).where(Competitor.id == competitor_id))
    ).scalar_one_or_none()
    if competitor is None:
        raise HTTPException(status_code=404, detail="Competitor not found")
    await db.delete(competitor)
    await db.flush()


@router.post("/competitive/competitors/{competitor_id}/battle-card", response_model=CompetitorRead)
async def generate_battle_card(
    competitor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """(Re)generate the AI battle card for a competitor."""
    competitor = (
        await db.execute(select(Competitor).where(Competitor.id == competitor_id))
    ).scalar_one_or_none()
    if competitor is None:
        raise HTTPException(status_code=404, detail="Competitor not found")

    ci = get_competitive_intelligence()
    if not ci.available:
        raise HTTPException(status_code=503, detail="AI unavailable (no API key configured)")
    await ci.refresh_battle_card(db, competitor)
    await db.refresh(competitor)
    return competitor


# --- Mentions ---

@router.get("/competitive/mentions", response_model=list[CompetitorMentionRead])
async def list_mentions(
    competitor_id: Optional[uuid.UUID] = Query(None),
    company_id: Optional[uuid.UUID] = Query(None),
    source: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    query = (
        select(CompetitorMention, Competitor.name)
        .join(Competitor, Competitor.id == CompetitorMention.competitor_id)
        .order_by(desc(CompetitorMention.detected_at))
    )
    if competitor_id:
        query = query.where(CompetitorMention.competitor_id == competitor_id)
    if company_id:
        query = query.where(CompetitorMention.company_id == company_id)
    if source:
        query = query.where(CompetitorMention.source == source)
    query = query.limit(limit)

    rows = (await db.execute(query)).all()
    out = []
    for mention, comp_name in rows:
        read = CompetitorMentionRead.model_validate(mention)
        read.competitor_name = comp_name
        out.append(read)
    return out


@router.post("/competitive/detect")
async def detect_mentions(
    payload: DetectRequest,
    db: AsyncSession = Depends(get_db),
):
    """Scan arbitrary text for tracked-competitor mentions and (optionally) record them."""
    ci = get_competitive_intelligence()
    found = await ci.detect_mentions_in_text(
        db,
        text=payload.text,
        source=payload.source,
        company_id=payload.company_id,
        contact_id=payload.contact_id,
        deal_id=payload.deal_id,
        persist=payload.persist,
    )
    return {"mentions_found": len(found), "mentions": found}


@router.get("/competitive/win-loss")
async def win_loss_correlation(
    db: AsyncSession = Depends(get_db),
):
    """Correlate competitor mentions with won vs lost deals at the same companies.

    For each competitor, counts deals (won/lost) at companies where the
    competitor has been mentioned — a rough competitive win-rate signal.
    """
    competitors = (await db.execute(select(Competitor))).scalars().all()
    results = []
    for comp in competitors:
        company_rows = await db.execute(
            select(CompetitorMention.company_id)
            .where(CompetitorMention.competitor_id == comp.id)
            .where(CompetitorMention.company_id.isnot(None))
            .distinct()
        )
        company_ids = [cid for (cid,) in company_rows.all() if cid is not None]
        if not company_ids:
            continue
        status_rows = await db.execute(
            select(Deal.status, func.count(Deal.id))
            .where(Deal.company_id.in_(company_ids))
            .group_by(Deal.status)
        )
        counts = {status: int(c) for status, c in status_rows.all()}
        won = counts.get("won", 0)
        lost = counts.get("lost", 0)
        total_closed = won + lost
        results.append({
            "competitor_id": str(comp.id),
            "competitor": comp.name,
            "companies_mentioned": len(company_ids),
            "deals_won": won,
            "deals_lost": lost,
            "win_rate": round(won / total_closed * 100, 1) if total_closed else None,
        })
    results.sort(key=lambda r: (r["deals_won"] + r["deals_lost"]), reverse=True)
    return {"competitors": results}
