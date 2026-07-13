"""Autonomous prospecting API (feature 3.1).

Trigger runs, inspect run history, view/learn the ICP, and work the human
review queue (approve → enroll, or reject) for borderline candidates.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.company import Company
from app.models.prospecting import IcpProfile, ProspectCandidate, ProspectingRun
from app.services.autonomous_prospector import get_prospector

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/prospecting", tags=["prospecting"])


# --- Schemas ---
class SeedCandidate(BaseModel):
    name: str
    domain: Optional[str] = None
    industry: Optional[str] = None
    employee_count: Optional[int] = None
    country: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    source: str = "manual"


class RunRequest(BaseModel):
    target_volume: Optional[int] = Field(None, ge=1, le=200)
    seed_candidates: Optional[list[SeedCandidate]] = None


class RunRead(BaseModel):
    id: uuid.UUID
    icp_profile_id: Optional[uuid.UUID] = None
    trigger: str
    status: str
    target_volume: int
    sources_queried: list
    companies_found: int
    companies_qualified: int
    companies_enrolled: int
    companies_review_queue: int
    results: dict
    error: Optional[str] = None
    started_at: datetime
    completed_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class CandidateRead(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    company_id: Optional[uuid.UUID] = None
    company_name: str
    domain: Optional[str] = None
    source: str
    fit_score: int
    qualification: dict
    reasoning: Optional[str] = None
    status: str
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class IcpRead(BaseModel):
    id: uuid.UUID
    name: str
    is_active: bool
    industry_weights: dict
    size_range: dict
    location_preferences: dict
    technology_signals: list
    pain_point_indicators: list
    exclusion_criteria: dict
    learned_from_deal_count: int
    model_used: Optional[str] = None
    updated_at: datetime

    class Config:
        from_attributes = True


@router.post("/run", response_model=RunRead)
async def trigger_run(payload: RunRequest, db: AsyncSession = Depends(get_db)):
    """Trigger an autonomous prospecting run now (manual)."""
    prospector = get_prospector()
    seeds = [c.model_dump() for c in payload.seed_candidates] if payload.seed_candidates else None
    run = await prospector.run(
        db, target_volume=payload.target_volume, seed_candidates=seeds, trigger="manual"
    )
    await db.flush()
    return run


@router.get("/runs", response_model=list[RunRead])
async def list_runs(
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(ProspectingRun).order_by(desc(ProspectingRun.started_at)).limit(limit)
        )
    ).scalars().all()
    return list(rows)


@router.get("/runs/{run_id}", response_model=RunRead)
async def get_run(run_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    run = (
        await db.execute(select(ProspectingRun).where(ProspectingRun.id == run_id))
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Prospecting run not found")
    return run


@router.get("/runs/{run_id}/candidates", response_model=list[CandidateRead])
async def run_candidates(run_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(
            select(ProspectCandidate)
            .where(ProspectCandidate.run_id == run_id)
            .order_by(desc(ProspectCandidate.fit_score))
        )
    ).scalars().all()
    return list(rows)


@router.get("/review-queue", response_model=list[CandidateRead])
async def review_queue(
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """Borderline candidates awaiting a human decision, best fit first."""
    rows = (
        await db.execute(
            select(ProspectCandidate)
            .where(ProspectCandidate.status == "review")
            .order_by(desc(ProspectCandidate.fit_score))
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


@router.post("/candidates/{candidate_id}/approve", response_model=CandidateRead)
async def approve_candidate(
    candidate_id: uuid.UUID,
    reviewer: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Approve a review-queue candidate → advance its company to outreach."""
    cand = (
        await db.execute(select(ProspectCandidate).where(ProspectCandidate.id == candidate_id))
    ).scalar_one_or_none()
    if cand is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    if cand.company_id is not None:
        company = (
            await db.execute(select(Company).where(Company.id == cand.company_id))
        ).scalar_one_or_none()
        if company is not None:
            company.pipeline_stage = "researching"
            await get_prospector()._auto_enroll(db, company)
    cand.status = "enrolled"
    cand.reviewed_by = reviewer
    cand.reviewed_at = datetime.now(timezone.utc)
    await db.flush()
    return cand


@router.post("/candidates/{candidate_id}/reject", response_model=CandidateRead)
async def reject_candidate(
    candidate_id: uuid.UUID,
    reviewer: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    cand = (
        await db.execute(select(ProspectCandidate).where(ProspectCandidate.id == candidate_id))
    ).scalar_one_or_none()
    if cand is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    cand.status = "rejected"
    cand.reviewed_by = reviewer
    cand.reviewed_at = datetime.now(timezone.utc)
    await db.flush()
    return cand


@router.get("/icp", response_model=Optional[IcpRead])
async def get_icp(db: AsyncSession = Depends(get_db)):
    """The active learned ICP (or null if none learned yet)."""
    icp = await get_prospector().get_active_icp(db)
    return icp


@router.post("/icp/learn", response_model=IcpRead)
async def learn_icp(db: AsyncSession = Depends(get_db)):
    """Refit the ICP from won-deal characteristics."""
    icp = await get_prospector().learn_icp(db)
    await db.flush()
    return icp
