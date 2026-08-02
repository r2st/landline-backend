"""Intelligent lead prioritization & routing endpoints (Feature 1.3).

Exposes propensity-ranked lead queues, per-company routing detail, and rep
profile management (expertise, win rates, capacity).
"""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.company import Company
from app.models.lead_score import LeadScore
from app.models.rep_profile import RepProfile
from app.models.user import User
from app.schemas.routing import (
    PrioritizedLead,
    PrioritizedLeadsResponse,
    RepProfileRead,
    RepProfileUpsert,
    RoutingDetail,
)
from app.services import lead_router

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/routing", tags=["routing"])

# Ranking blend — propensity ("convert now") weighted over fit ("good lead").
_FIT_WEIGHT = 0.4
_PROPENSITY_WEIGHT = 0.6


def _priority_score(fit: int, propensity: int) -> float:
    return round(_FIT_WEIGHT * fit + _PROPENSITY_WEIGHT * propensity, 2)


def _latest_score_subquery():
    """Latest LeadScore.scored_at per company."""
    return (
        select(LeadScore.company_id, func.max(LeadScore.scored_at).label("max_at"))
        .group_by(LeadScore.company_id)
        .subquery()
    )


async def _rep_names(db: AsyncSession, ids: set) -> dict:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    rows = await db.execute(select(User.id, User.name).where(User.id.in_(ids)))
    return {uid: name for uid, name in rows.all()}


@router.get("/prioritized", response_model=PrioritizedLeadsResponse)
async def prioritized_leads(
    min_propensity: int = Query(0, ge=0, le=100),
    active_only: bool = Query(True, description="Only leads in an active pipeline stage"),
    limit: int = Query(25, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Leads ranked by a blended fit + propensity priority score (timing-weighted)."""
    latest = _latest_score_subquery()
    query = (
        select(LeadScore, Company)
        .join(latest, (LeadScore.company_id == latest.c.company_id) & (LeadScore.scored_at == latest.c.max_at))
        .join(Company, Company.id == LeadScore.company_id)
        .where(LeadScore.propensity_score >= min_propensity)
    )
    if active_only:
        query = query.where(
            Company.pipeline_stage.in_(("researching", "outreach", "engaged", "negotiating"))
        )
    rows = (await db.execute(query)).all()

    rep_ids = {ls.recommended_rep_id for ls, _ in rows}
    rep_names = await _rep_names(db, rep_ids)

    leads = []
    for ls, company in rows:
        leads.append(PrioritizedLead(
            company_id=company.id,
            company_name=company.name,
            fit_score=ls.fit_score,
            propensity_score=ls.propensity_score,
            signal_velocity=ls.signal_velocity,
            priority_score=_priority_score(ls.fit_score, ls.propensity_score),
            last_signal_at=ls.last_signal_at,
            recommended_rep_id=ls.recommended_rep_id,
            recommended_rep_name=rep_names.get(ls.recommended_rep_id),
            pipeline_stage=company.pipeline_stage,
        ))
    leads.sort(key=lambda x: x.priority_score, reverse=True)
    total = len(leads)
    return PrioritizedLeadsResponse(leads=leads[:limit], total=total)


@router.post("/company/{company_id}/score", response_model=RoutingDetail)
async def score_and_route_company(company_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Run fit + propensity scoring and rep matching for one company, storing a LeadScore."""
    company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    ls = await lead_router.score_and_route(db, company)
    rep_name = None
    if ls.recommended_rep_id:
        rep_name = (
            await db.execute(select(User.name).where(User.id == ls.recommended_rep_id))
        ).scalar_one_or_none()
    return RoutingDetail(
        company_id=company.id,
        company_name=company.name,
        fit_score=ls.fit_score,
        propensity_score=ls.propensity_score,
        signal_velocity=ls.signal_velocity,
        last_signal_at=ls.last_signal_at,
        recommended_rep_id=ls.recommended_rep_id,
        recommended_rep_name=rep_name,
        pipeline_stage=company.pipeline_stage,
        factors=ls.factors or {},
        scored_at=ls.scored_at,
    )


@router.get("/company/{company_id}", response_model=RoutingDetail)
async def company_routing(company_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Latest routing detail for a company (scores it now if never scored)."""
    company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    latest = _latest_score_subquery()
    ls = (
        await db.execute(
            select(LeadScore)
            .join(latest, (LeadScore.company_id == latest.c.company_id) & (LeadScore.scored_at == latest.c.max_at))
            .where(LeadScore.company_id == company_id)
            # The join is on ``scored_at == MAX(scored_at)``, which selects the
            # latest score — and every score sharing that timestamp. Two rescores
            # in the same tick tie, and a tie made this a 500 rather than a
            # routing answer. Order for determinism, then take one.
            .order_by(LeadScore.id.asc())
            .limit(1)
        )
    ).scalars().first()
    if ls is None:
        ls = await lead_router.score_and_route(db, company)

    rep_name = None
    if ls.recommended_rep_id:
        rep_name = (
            await db.execute(select(User.name).where(User.id == ls.recommended_rep_id))
        ).scalar_one_or_none()
    return RoutingDetail(
        company_id=company.id,
        company_name=company.name,
        fit_score=ls.fit_score,
        propensity_score=ls.propensity_score,
        signal_velocity=ls.signal_velocity,
        last_signal_at=ls.last_signal_at,
        recommended_rep_id=ls.recommended_rep_id,
        recommended_rep_name=rep_name,
        pipeline_stage=company.pipeline_stage,
        factors=ls.factors or {},
        scored_at=ls.scored_at,
    )


# --- Rep profiles ---------------------------------------------------------

async def _serialize_profile(db: AsyncSession, p: RepProfile, name: Optional[str] = None) -> dict:
    if name is None:
        name = (await db.execute(select(User.name).where(User.id == p.user_id))).scalar_one_or_none()
    out = RepProfileRead.model_validate(p).model_dump()
    out["user_name"] = name
    return out


@router.get("/reps", response_model=list[RepProfileRead])
async def list_rep_profiles(db: AsyncSession = Depends(get_db)):
    """List all rep profiles with their expertise, win rates, and capacity."""
    rows = (await db.execute(select(RepProfile, User.name).join(User, User.id == RepProfile.user_id))).all()
    return [await _serialize_profile(db, p, name) for p, name in rows]


@router.put("/reps/{user_id}", response_model=RepProfileRead)
async def upsert_rep_profile(
    user_id: uuid.UUID, payload: RepProfileUpsert, db: AsyncSession = Depends(get_db)
):
    """Create or update a rep's profile (industries, capacity). Recomputes win rates."""
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    profile = (
        await db.execute(select(RepProfile).where(RepProfile.user_id == user_id))
    ).scalar_one_or_none()
    if profile is None:
        profile = RepProfile(user_id=user_id)
        db.add(profile)

    changes = payload.model_dump(exclude_unset=True)
    if "industries" in changes and changes["industries"] is not None:
        profile.industries = changes["industries"]
    if "max_capacity" in changes and changes["max_capacity"] is not None:
        profile.max_capacity = changes["max_capacity"]
    await db.flush()

    # Recompute derived stats from the rep's closed deals.
    await lead_router.recompute_rep_profile(db, profile)
    # Load server-default columns (created_at/updated_at) before serialization.
    await db.refresh(profile)
    return await _serialize_profile(db, profile, user.name)


@router.post("/reps/recompute")
async def recompute_rep_profiles(db: AsyncSession = Depends(get_db)):
    """Recompute win-rate-by-segment, aggregate rates, velocity, and load for all reps."""
    profiles = list((await db.execute(select(RepProfile))).scalars().all())
    for p in profiles:
        await lead_router.recompute_rep_profile(db, p)
    return {"recomputed": len(profiles)}
