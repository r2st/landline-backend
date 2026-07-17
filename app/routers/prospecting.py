"""Autonomous prospecting API (feature 3.1).

Trigger runs, inspect run history, view/edit/learn the ICP, work the human
review queue (approve → enroll, or reject) for borderline candidates, and trace
a company back to the candidate that put it in the pipeline.
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
from app.dependencies import get_current_user
from app.models.company import Company
from app.models.prospecting import (
    CANDIDATE_STATUSES,
    IcpProfile,
    ProspectCandidate,
    ProspectingRun,
)
from app.models.user import User
from app.services import api_credits
from app.services.autonomous_prospector import (
    OVERRIDABLE_ICP_FIELDS,
    apply_manual_overrides,
    get_prospector,
)
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
    # Which fields a human pinned, and who. The UI needs these to answer "will
    # Monday's re-learn undo what I just set?" — without them, an operator has
    # no way to tell a learned weight from one they chose.
    manual_overrides: dict = {}
    edited_at: Optional[datetime] = None
    edited_by: Optional[str] = None

    class Config:
        from_attributes = True


class IcpUpdate(BaseModel):
    """Pin one or more ICP fields to human-chosen values.

    Every field is optional and only what's sent is pinned — omitting a field
    leaves it learned and free to keep improving. To hand a field back to the
    learner, send it in ``unpin`` rather than guessing a value to write.
    """

    industry_weights: Optional[dict] = None
    size_range: Optional[dict] = None
    location_preferences: Optional[dict] = None
    technology_signals: Optional[list] = None
    pain_point_indicators: Optional[list] = None
    exclusion_criteria: Optional[dict] = None
    # Field names to release back to the learner, applied after any pins above.
    unpin: list[str] = Field(default_factory=list)


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


class ProvenanceRun(BaseModel):
    """The run that surfaced a company, trimmed to what a detail page needs."""

    id: uuid.UUID
    trigger: str
    status: str
    started_at: datetime
    sources_queried: list = []


class CompanyProvenance(BaseModel):
    """Why a company is in the pipeline at all.

    Answers the question a company detail page could not: an agent-discovered
    lead showed a `source` string and nothing else — no fit score, no rationale,
    no link to the run that found it.
    """

    company_id: uuid.UUID
    # False when nothing in prospecting created this company — a manual add, a
    # CSV import, a business card. Distinguishing that from "the agent found it
    # but we lost the record" matters: only one of them is a bug.
    discovered_by_agent: bool
    candidate_id: Optional[uuid.UUID] = None
    source: Optional[str] = None
    fit_score: Optional[int] = None
    qualification: dict = {}
    reasoning: Optional[str] = None
    status: Optional[str] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    discovered_at: Optional[datetime] = None
    dedup_confidence: Optional[str] = None
    run: Optional[ProvenanceRun] = None


@router.get("/companies/{company_id}/provenance", response_model=CompanyProvenance)
async def company_provenance(company_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Trace a company back to the prospect candidate that created it.

    Returns ``discovered_by_agent: false`` rather than 404 for a company the
    agent never touched — "this lead came in some other way" is a real answer,
    and a 404 would make the caller guess between that and a broken lookup.
    """
    company = (
        await db.execute(select(Company).where(Company.id == company_id))
    ).scalar_one_or_none()
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")

    # Newest first: a company can be re-surfaced by later runs, and the most
    # recent decision is the one that explains its current state.
    candidate = (
        await db.execute(
            select(ProspectCandidate)
            .where(ProspectCandidate.company_id == company_id)
            .order_by(desc(ProspectCandidate.created_at))
            .limit(1)
        )
    ).scalar_one_or_none()

    if candidate is None:
        return CompanyProvenance(company_id=company_id, discovered_by_agent=False)

    # run_id is NOT NULL with ON DELETE CASCADE, so an existing candidate always
    # has its run. Still tolerate a miss rather than 500 a detail page over
    # provenance trivia.
    row = (
        await db.execute(
            select(ProspectingRun).where(ProspectingRun.id == candidate.run_id)
        )
    ).scalar_one_or_none()
    run = (
        ProvenanceRun(
            id=row.id,
            trigger=row.trigger,
            status=row.status,
            started_at=row.started_at,
            sources_queried=row.sources_queried or [],
        )
        if row is not None
        else None
    )

    return CompanyProvenance(
        company_id=company_id,
        discovered_by_agent=True,
        candidate_id=candidate.id,
        source=candidate.source,
        fit_score=candidate.fit_score,
        qualification=candidate.qualification or {},
        reasoning=candidate.reasoning,
        status=candidate.status,
        reviewed_by=candidate.reviewed_by,
        reviewed_at=candidate.reviewed_at,
        discovered_at=candidate.created_at,
        dedup_confidence=candidate.dedup_confidence,
        run=run,
    )


@router.get("/icp", response_model=Optional[IcpRead])
async def get_icp(db: AsyncSession = Depends(get_db)):
    """The active learned ICP (or null if none learned yet)."""
    icp = await get_prospector().get_active_icp(db)
    return icp


@router.put("/icp", response_model=IcpRead)
async def update_icp(
    payload: IcpUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Pin ICP fields to human-chosen values.

    Writes the value *and* records the field as manually owned, so the weekly
    re-learn re-applies it instead of reverting it. Fields left alone stay
    learned and keep tracking won deals; fields listed in ``unpin`` are handed
    back to the learner and take their learned value at the next fit.

    Updates the active profile in place rather than creating one: a pin is a
    correction to the current ICP, not a new hypothesis, and inserting a profile
    per edit would make ``learned_from_deal_count`` meaningless.
    """
    prospector = get_prospector()
    # An edit needs something to edit. `ensure_icp` fits a profile from won
    # deals (or the defaults) rather than 404ing on a fresh install.
    icp = await prospector.ensure_icp(db)

    pins = payload.model_dump(exclude_none=True, exclude={"unpin"})
    unknown_unpin = set(payload.unpin) - OVERRIDABLE_ICP_FIELDS
    if unknown_unpin:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot unpin unknown field(s): {sorted(unknown_unpin)}. "
                f"Overridable fields are {sorted(OVERRIDABLE_ICP_FIELDS)}."
            ),
        )

    overrides = dict(icp.manual_overrides or {})
    for field, value in pins.items():
        overrides[field] = value
    for field in payload.unpin:
        overrides.pop(field, None)

    icp.manual_overrides = overrides
    if pins or payload.unpin:
        icp.edited_at = datetime.now(timezone.utc)
        icp.edited_by = user.email

    # Re-apply so the response reflects what the agent will actually score on,
    # not what was learned before this edit.
    apply_manual_overrides(icp)
    # An unpinned field keeps its last value until the next fit — say so in the
    # log rather than implying the learned value comes back instantly.
    if payload.unpin:
        logger.info(
            "ICP %s: %s released to the learner by %s; they keep their current "
            "values until the next re-learn.",
            icp.id, sorted(payload.unpin), user.email,
        )
    await db.flush()
    return icp


@router.post("/icp/learn", response_model=IcpRead)
async def learn_icp(db: AsyncSession = Depends(get_db)):
    """Refit the ICP from won-deal characteristics.

    Pinned fields survive: the fit is recomputed in full, then any manual
    overrides are re-applied on top and carried onto the new profile.
    """
    icp = await get_prospector().learn_icp(db)
    await db.flush()
    return icp
