"""Lead scoring, pipeline view, and consent management endpoints."""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import COUNTRY_QUERY_PATTERN
from app.database import get_db
from app.models.company import Company
from app.models.contact import Contact
from app.models.lead_score import LeadScore
from app.schemas.lead import (
    ConsentCreate,
    ConsentRead,
    LeadPipelineItem,
    LeadPipelineResponse,
    LeadScoreBatchRequest,
    LeadScoreBatchResponse,
    LeadScoreRead,
    LeadScoreRequest,
)
from app.services.scoring import score_company_rule_based
from app.services.consent import check_consent, record_consent, revoke_consent

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/leads", tags=["leads"])


@router.post("/score", response_model=LeadScoreRead)
async def score_company(
    payload: LeadScoreRequest,
    db: AsyncSession = Depends(get_db),
):
    """Score a single company using rule-based scoring."""
    result = await db.execute(
        select(Company).where(Company.id == payload.company_id)
    )
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    score = await score_company_rule_based(db, company)

    # Update company stage if still 'new' or 'researched'
    if company.stage in ("new", "researched"):
        company.stage = "scored"

    return score


@router.post("/score/batch", response_model=LeadScoreBatchResponse)
async def score_companies_batch(
    payload: LeadScoreBatchRequest,
    db: AsyncSession = Depends(get_db),
):
    """Score multiple companies in one request."""
    scored = 0
    errors = []

    for company_id in payload.company_ids:
        try:
            result = await db.execute(
                select(Company).where(Company.id == company_id)
            )
            company = result.scalar_one_or_none()
            if not company:
                errors.append({"company_id": str(company_id), "error": "not found"})
                continue

            await score_company_rule_based(db, company)
            if company.stage in ("new", "researched"):
                company.stage = "scored"
            scored += 1

        except Exception as e:
            errors.append({"company_id": str(company_id), "error": str(e)})

    return LeadScoreBatchResponse(scored=scored, errors=errors)


@router.get("/pipeline", response_model=LeadPipelineResponse)
async def get_pipeline(
    stage: Optional[str] = Query(None),
    min_score: Optional[int] = Query(None, ge=0, le=100),
    country: Optional[str] = Query(None, pattern=COUNTRY_QUERY_PATTERN),
    sort_by: Optional[str] = Query("fit_score", description="fit_score, name, stage"),
    sort_dir: Optional[str] = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Get the lead pipeline with scores, filtering, and sorting."""
    # Subquery: latest rule-based score per company
    latest_score = (
        select(
            LeadScore.company_id,
            func.max(LeadScore.scored_at).label("max_scored_at"),
        )
        .where(LeadScore.score_type == "rule_based")
        .group_by(LeadScore.company_id)
        .subquery()
    )

    query = (
        select(
            Company.id.label("company_id"),
            Company.name.label("company_name"),
            Company.domain,
            Company.stage,
            Company.country,
            Company.employee_count,
            Company.industry_segment,
            Company.source,
            Company.city,
            LeadScore.fit_score,
        )
        .outerjoin(
            latest_score,
            Company.id == latest_score.c.company_id,
        )
        .outerjoin(
            LeadScore,
            and_(
                LeadScore.company_id == Company.id,
                LeadScore.scored_at == latest_score.c.max_scored_at,
                LeadScore.score_type == "rule_based",
            ),
        )
    )

    count_query = select(func.count(Company.id))

    conditions = []
    if stage:
        conditions.append(Company.stage == stage)
    if min_score is not None:
        conditions.append(LeadScore.fit_score >= min_score)
    if country:
        conditions.append(Company.country == country)

    if conditions:
        combined = and_(*conditions)
        query = query.where(combined)
        count_query = count_query.where(combined)

    total_result = await db.execute(count_query)
    total = total_result.scalar()

    # Sort
    if sort_by == "fit_score":
        sort_col = LeadScore.fit_score
    elif sort_by == "name":
        sort_col = Company.name
    else:
        sort_col = Company.stage

    if sort_dir == "asc":
        query = query.order_by(sort_col.asc().nullslast())
    else:
        query = query.order_by(sort_col.desc().nullslast())

    offset = (page - 1) * page_size
    query = query.offset(offset).limit(page_size)

    result = await db.execute(query)
    rows = result.all()

    items = [
        LeadPipelineItem(
            company_id=row.company_id,
            company_name=row.company_name,
            domain=row.domain,
            stage=row.stage,
            fit_score=row.fit_score,
            country=row.country,
            employee_count=row.employee_count,
            industry_segment=row.industry_segment,
            source=row.source,
            city=row.city,
        )
        for row in rows
    ]

    return LeadPipelineResponse(
        items=items, total=total, page=page, page_size=page_size
    )


@router.post("/consent", response_model=ConsentRead, status_code=201)
async def create_consent(
    payload: ConsentCreate,
    db: AsyncSession = Depends(get_db),
):
    """Record consent for a contact."""
    result = await db.execute(
        select(Contact).where(Contact.id == payload.contact_id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Contact not found")

    record = await record_consent(
        db,
        contact_id=payload.contact_id,
        consent_type=payload.consent_type,
        basis=payload.basis,
        evidence_url=payload.evidence_url,
        granted_at=payload.granted_at,
        expires_at=payload.expires_at,
    )
    return record


@router.get("/consent/{contact_id}")
async def get_contact_consent(
    contact_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Get consent status for a contact."""
    result = await db.execute(
        select(Contact).where(Contact.id == contact_id)
    )
    contact = result.scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    # Load company for regime check
    result = await db.execute(
        select(Company).where(Company.id == contact.company_id)
    )
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    consent_check = await check_consent(db, contact, company)
    return consent_check.to_dict()


@router.post("/consent/{consent_id}/revoke", response_model=ConsentRead)
async def revoke_consent_endpoint(
    consent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Revoke an existing consent record."""
    try:
        record = await revoke_consent(db, consent_id)
        return record
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
