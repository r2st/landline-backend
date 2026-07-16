"""Company discovery and enrichment endpoints."""

import uuid
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models.company import Company
from app.services.enrichment import get_enrichment_service
from app.services.ingestion import get_ingestion_service

router = APIRouter(prefix="/discovery", tags=["discovery"])


class CompanySearchRequest(BaseModel):
    industry: str = Field("", description="Industry keyword, e.g. 'injection molding'")
    location: str = Field("", description="Location, e.g. 'Ohio, United States'")
    keywords: list[str] = Field(default_factory=list, description="Additional keyword filters")
    min_employees: Optional[int] = None
    max_employees: Optional[int] = None
    per_page: int = Field(10, ge=1, le=25)


class EnrichCompanyRequest(BaseModel):
    find_contacts: bool = Field(True, description="Also search for contacts at this company")
    contact_titles: list[str] = Field(
        default_factory=list,
        description="Job titles to search for (empty = use defaults)"
    )


@router.post("/search-companies")
async def search_companies(
    payload: CompanySearchRequest,
    db: AsyncSession = Depends(get_db),
):
    """Search Apollo for companies matching criteria and import them.

    Two steps now, deliberately: Apollo returns records, the ingestion service
    persists them under the one shared dedup rule. ``get_db`` owns the commit —
    the service used to commit mid-request on its own.
    """
    service = get_enrichment_service()
    search = await service.search_companies(
        db,
        industry=payload.industry,
        location=payload.location,
        keywords=payload.keywords,
        min_employees=payload.min_employees,
        max_employees=payload.max_employees,
        per_page=payload.per_page,
        target_volume=payload.per_page,
    )
    result = await get_ingestion_service().ingest(db, search.records)

    companies = []
    for outcome in result.outcomes:
        companies.append({
            "name": outcome.record.name,
            "domain": outcome.record.domain,
            "status": "imported" if outcome.created else (
                "error" if outcome.error else "exists"
            ),
            "id": str(outcome.company_id) if outcome.company_id else None,
            "industry": outcome.record.industry,
            "employee_count": outcome.record.employee_count,
            "city": outcome.record.city,
            "state": outcome.record.state,
            "dedup_confidence": outcome.dedup.confidence,
            "error": outcome.error,
        })
    return {
        "total": len(companies),
        "imported": result.companies_created,
        "already_existed": result.companies_matched,
        # 'ok' | 'rate_limited' | 'budget_exhausted' | 'error' | 'skipped' —
        # a rate-limited search must not read as "found nothing".
        "search_status": search.status,
        "credits_spent": search.credits_spent,
        "errors": result.errors,
        "companies": companies,
    }


@router.post("/enrich/{company_id}")
async def enrich_company(
    company_id: uuid.UUID,
    payload: EnrichCompanyRequest,
    db: AsyncSession = Depends(get_db),
):
    """Enrich a company with Apollo data and optionally find contacts."""
    result = await db.execute(
        select(Company).where(Company.id == company_id)
    )
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    service = get_enrichment_service()
    enrich_result = await service.enrich_company(db, company)

    contacts_found = []
    if payload.find_contacts:
        titles = payload.contact_titles if payload.contact_titles else None
        contacts_found = await service.find_contacts(db, company, titles=titles)

    await db.commit()
    return {
        "enrichment": enrich_result,
        "contacts_found": contacts_found,
        "contacts_count": len(contacts_found),
    }



@router.get("/send-stats")
async def get_send_stats(db: AsyncSession = Depends(get_db)):
    """Get current email sending statistics and limits."""
    from app.services.send_limiter import get_send_limiter
    limiter = get_send_limiter()
    return await limiter.get_send_stats(db)


@router.post("/scrape-website")
async def scrape_website(
    payload: dict,  # {"domain": "example.com"}
    db: AsyncSession = Depends(get_db),
):
    """Scrape a company's website for contact info and metadata."""
    from app.services.web_scraper import get_web_scraper
    domain = payload.get("domain", "").strip()
    if not domain:
        raise HTTPException(status_code=400, detail="domain is required")
    scraper = get_web_scraper()
    return await scraper.scrape_company(domain)


@router.get("/send-timing")
async def get_send_timing(db: AsyncSession = Depends(get_db)):
    """Get optimal email send timing analysis."""
    from app.services.send_timing import get_send_timing as get_timing
    analyzer = get_timing()
    return await analyzer.get_optimal_times(db)
