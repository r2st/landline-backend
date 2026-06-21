"""Company CRUD, search, and pipeline management endpoints."""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.company import Company
from app.models.contact import Contact
from app.models.lead_score import LeadScore
from app.schemas.company import (
    CompanyCreate,
    CompanyListResponse,
    CompanyRead,
    CompanyStageUpdate,
    CompanyUpdate,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/companies", tags=["companies"])


async def _attach_extras(db: AsyncSession, companies: list[Company]) -> None:
    """Attach derived ``fit_score`` (latest rule-based) and ``contact_count``
    onto company instances so they serialize into CompanyRead. Bulk-queried to
    avoid N+1."""
    if not companies:
        return
    ids = [c.id for c in companies]

    # Latest rule-based fit score per company.
    latest = (
        select(
            LeadScore.company_id,
            func.max(LeadScore.scored_at).label("max_scored_at"),
        )
        .where(LeadScore.company_id.in_(ids), LeadScore.score_type == "rule_based")
        .group_by(LeadScore.company_id)
        .subquery()
    )
    score_rows = await db.execute(
        select(LeadScore.company_id, LeadScore.fit_score)
        .join(
            latest,
            and_(
                LeadScore.company_id == latest.c.company_id,
                LeadScore.scored_at == latest.c.max_scored_at,
            ),
        )
        .where(LeadScore.score_type == "rule_based")
    )
    score_by_company = {row[0]: row[1] for row in score_rows.all()}

    # Contact counts per company.
    count_rows = await db.execute(
        select(Contact.company_id, func.count(Contact.id))
        .where(Contact.company_id.in_(ids))
        .group_by(Contact.company_id)
    )
    count_by_company = {row[0]: row[1] for row in count_rows.all()}

    for c in companies:
        c.fit_score = score_by_company.get(c.id)
        c.contact_count = count_by_company.get(c.id, 0)


@router.post("/", response_model=CompanyRead, status_code=201)
async def create_company(
    payload: CompanyCreate,
    db: AsyncSession = Depends(get_db),
):
    """Create a new company record."""
    company = Company(**payload.model_dump())
    db.add(company)
    await db.flush()
    await db.refresh(company)
    logger.info("Created company %s (%s)", company.name, company.id)
    return company


@router.get("/", response_model=CompanyListResponse)
async def list_companies(
    q: Optional[str] = Query(None, description="Search name, domain, or industry"),
    country: Optional[str] = Query(None, pattern="^(US|CA)$"),
    stage: Optional[str] = Query(None),
    naics_code: Optional[str] = Query(None),
    industry_segment: Optional[str] = Query(None),
    min_employees: Optional[int] = Query(None, ge=0),
    max_employees: Optional[int] = Query(None, ge=0),
    tags: Optional[str] = Query(None, description="Comma-separated tags"),
    sort_by: Optional[str] = Query("created_at", description="Sort field"),
    sort_dir: Optional[str] = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """List companies with filtering, search, and pagination."""
    query = select(Company)
    count_query = select(func.count(Company.id))

    # Filters
    conditions = []
    if q:
        search = f"%{q}%"
        conditions.append(
            or_(
                Company.name.ilike(search),
                Company.domain.ilike(search),
                Company.industry_segment.ilike(search),
                Company.naics_description.ilike(search),
            )
        )
    if country:
        conditions.append(Company.country == country)
    if stage:
        conditions.append(Company.stage == stage)
    if naics_code:
        conditions.append(Company.naics_code.startswith(naics_code))
    if industry_segment:
        conditions.append(Company.industry_segment.ilike(f"%{industry_segment}%"))
    if min_employees is not None:
        conditions.append(Company.employee_count >= min_employees)
    if max_employees is not None:
        conditions.append(Company.employee_count <= max_employees)
    if tags:
        tag_list = [t.strip() for t in tags.split(",")]
        for tag in tag_list:
            conditions.append(Company.tags.any(tag))

    if conditions:
        combined = and_(*conditions)
        query = query.where(combined)
        count_query = count_query.where(combined)

    # Count
    total_result = await db.execute(count_query)
    total = total_result.scalar()

    # Sort — whitelist allowed columns to prevent attribute access abuse
    ALLOWED_SORTS = {"name", "created_at", "stage", "country", "employee_count", "city", "domain"}
    sort_column = getattr(Company, sort_by) if sort_by in ALLOWED_SORTS else Company.created_at
    if sort_dir == "asc":
        query = query.order_by(sort_column.asc())
    else:
        query = query.order_by(sort_column.desc())

    # Paginate
    offset = (page - 1) * page_size
    query = query.offset(offset).limit(page_size)

    result = await db.execute(query)
    companies = list(result.scalars().all())

    await _attach_extras(db, companies)

    return CompanyListResponse(
        items=companies,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{company_id}", response_model=CompanyRead)
async def get_company(
    company_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Get a single company by ID."""
    result = await db.execute(select(Company).where(Company.id == company_id))
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    await _attach_extras(db, [company])
    return company


@router.patch("/{company_id}", response_model=CompanyRead)
async def update_company(
    company_id: uuid.UUID,
    payload: CompanyUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update company fields."""
    result = await db.execute(select(Company).where(Company.id == company_id))
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    update_data = payload.model_dump(exclude_unset=True)
    if "stage" in update_data:
        from app.schemas.company import VALID_STAGES
        if update_data["stage"] not in VALID_STAGES:
            raise HTTPException(status_code=400, detail=f"Invalid stage. Must be one of: {VALID_STAGES}")

    for field, value in update_data.items():
        setattr(company, field, value)

    await db.flush()
    await db.refresh(company)
    logger.info("Updated company %s", company_id)
    return company


@router.patch("/{company_id}/stage", response_model=CompanyRead)
async def update_company_stage(
    company_id: uuid.UUID,
    payload: CompanyStageUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Transition a company to a new pipeline stage."""
    result = await db.execute(select(Company).where(Company.id == company_id))
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    old_stage = company.stage
    company.stage = payload.stage
    await db.flush()
    await db.refresh(company)
    logger.info("Company %s stage: %s -> %s", company_id, old_stage, payload.stage)
    return company


@router.delete("/{company_id}", status_code=204)
async def delete_company(
    company_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Delete a company and all related data (cascading)."""
    result = await db.execute(select(Company).where(Company.id == company_id))
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    await db.delete(company)
    await db.flush()
    logger.info("Deleted company %s (%s)", company.name, company_id)


@router.get("/{company_id}/scores")
async def get_company_scores(
    company_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Get all lead scores for a company."""
    result = await db.execute(select(Company).where(Company.id == company_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Company not found")

    scores_result = await db.execute(
        select(LeadScore)
        .where(LeadScore.company_id == company_id)
        .order_by(LeadScore.scored_at.desc())
    )
    scores = list(scores_result.scalars().all())
    return scores
