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
from app.models.prospecting import (
    CANDIDATE_STATUSES,
    IcpProfile,
    ProspectCandidate,
    ProspectingRun,
)
from app.services import api_credits
from app.services.autonomous_prospector import get_prospector
from app.services.company_contacts import display_name, resolve_primary_contacts

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
    dedup_confidence: Optional[str] = None
    merged_into_company_id: Optional[uuid.UUID] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    created_at: datetime
    # The normalized source record behind this candidate: industry (for Places,
    # the search query that surfaced it), city/state/country, employee_count.
    # A reviewer can't judge a company from a name and a number alone.
    raw_data: dict = {}
    # Whether the candidate's company has anyone to email. Derived rather than
    # stored: `raw_data` carries no email (Places attaches it to the contact,
    # not the company dict), and this answers the question a reviewer is really
    # asking — is there someone to enrol?
    has_email: bool = False
    # The address itself, plus who it belongs to. "Yes" alone tells a reviewer
    # an email exists but not whether it's worth enrolling — a generic
    # info@ inbox and a named owner are very different decisions.
    contact_email: Optional[str] = None
    contact_name: Optional[str] = None
    contact_title: Optional[str] = None
    contact_phone: Optional[str] = None

    class Config:
        from_attributes = True


async def _to_reads(
    db: AsyncSession, candidates: list[ProspectCandidate]
) -> list[CandidateRead]:
    """Serialize candidates with their contact resolved for the whole batch."""
    primaries = await resolve_primary_contacts(
        db, [c.company_id for c in candidates if c.company_id is not None]
    )
    reads = []
    for c in candidates:
        contact = primaries.get(c.company_id) if c.company_id else None
        reads.append(
            CandidateRead.model_validate(c).model_copy(
                update={
                    "has_email": bool(contact and contact.email),
                    "contact_email": contact.email if contact else None,
                    "contact_name": display_name(contact) if contact else None,
                    "contact_title": contact.title if contact else None,
                    "contact_phone": (
                        (contact.phone or contact.mobile) if contact else None
                    ),
                }
            )
        )
    return reads


class MergeCandidateRequest(BaseModel):
    """Confirm that a medium-confidence match really is the same company."""

    target_company_id: uuid.UUID


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
    return await _to_reads(db, list(rows))


@router.get("/review-queue", response_model=list[CandidateRead])
async def review_queue(
    limit: int = Query(100, ge=1, le=500),
    status: str = Query(
        "review",
        description="Candidate disposition to list. 'review' (the default) is "
        "the pending queue; 'enrolled' is what a reviewer approved.",
    ),
    sort: str = Query("fit_score", pattern="^(fit_score|created_at)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    db: AsyncSession = Depends(get_db),
):
    """Candidates by disposition — by default the ones awaiting a decision.

    Sorting is applied here rather than in the client because `limit` truncates:
    sorting a fit-score-ordered page by date client-side would only ever reorder
    the top `limit` by fit, which is not the newest `limit`.
    """
    if status not in CANDIDATE_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of {', '.join(CANDIDATE_STATUSES)}",
        )
    column = ProspectCandidate.fit_score if sort == "fit_score" else ProspectCandidate.created_at
    rows = (
        await db.execute(
            select(ProspectCandidate)
            .where(ProspectCandidate.status == status)
            .order_by(desc(column) if order == "desc" else column)
            .limit(limit)
        )
    ).scalars().all()
    return await _to_reads(db, list(rows))


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
    return (await _to_reads(db, [cand]))[0]


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
    return (await _to_reads(db, [cand]))[0]


@router.get("/candidates/duplicates", response_model=list[CandidateRead])
async def duplicate_candidates(
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Name-only dedup matches awaiting a human merge decision.

    These are candidates the pipeline refused to auto-merge: a shared company
    name with nothing to corroborate it. "Precision Manufacturing" in Ohio and
    in Pune land here rather than being silently folded into one row.
    """
    rows = (
        await db.execute(
            select(ProspectCandidate)
            .where(
                ProspectCandidate.dedup_confidence == "medium",
                ProspectCandidate.merged_into_company_id.is_(None),
            )
            .order_by(desc(ProspectCandidate.created_at))
            .limit(limit)
        )
    ).scalars().all()
    return await _to_reads(db, list(rows))


@router.post("/candidates/{candidate_id}/merge", response_model=CandidateRead)
async def merge_candidate(
    candidate_id: uuid.UUID,
    payload: MergeCandidateRequest,
    reviewer: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Confirm a merge the pipeline declined to make on its own."""
    cand = (
        await db.execute(
            select(ProspectCandidate).where(ProspectCandidate.id == candidate_id)
        )
    ).scalar_one_or_none()
    if cand is None:
        raise HTTPException(status_code=404, detail="Candidate not found")

    target = (
        await db.execute(
            select(Company).where(Company.id == payload.target_company_id)
        )
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="Target company not found")

    cand.merged_into_company_id = target.id
    cand.dedup_confidence = "certain"  # a human confirmed identity
    cand.reviewed_by = reviewer
    cand.reviewed_at = datetime.now(timezone.utc)
    await db.flush()
    return (await _to_reads(db, [cand]))[0]


@router.get("/credits")
async def credit_usage(db: AsyncSession = Depends(get_db)):
    """Apollo spend for today and this month against budget.

    The daily enrollment cap bounds *enrollments*, not *API calls* — this is the
    meter for what the loop actually spends.
    """
    status = await api_credits.get_budget_status(db, "apollo", cost=0)
    return status.as_dict()


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
