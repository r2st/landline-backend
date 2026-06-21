"""Company discovery and enrichment endpoints."""

from typing import Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models.company import Company
from app.services.enrichment import get_enrichment_service

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
    """Search Apollo for companies matching criteria and import them."""
    service = get_enrichment_service()
    results = await service.search_companies(
        db,
        industry=payload.industry,
        location=payload.location,
        keywords=payload.keywords,
        min_employees=payload.min_employees,
        max_employees=payload.max_employees,
        per_page=payload.per_page,
    )
    imported_count = sum(1 for r in results if r["status"] == "imported")
    existing_count = sum(1 for r in results if r["status"] == "exists")
    return {
        "total": len(results),
        "imported": imported_count,
        "already_existed": existing_count,
        "companies": results,
    }


@router.post("/enrich/{company_id}")
async def enrich_company(
    company_id: str,
    payload: EnrichCompanyRequest,
    db: AsyncSession = Depends(get_db),
):
    """Enrich a company with Apollo data and optionally find contacts."""
    from uuid import UUID
    result = await db.execute(
        select(Company).where(Company.id == UUID(company_id))
    )
    company = result.scalar_one_or_none()
    if not company:
        from fastapi import HTTPException
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
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="domain is required")
    scraper = get_web_scraper()
    return await scraper.scrape_company(domain)


@router.get("/send-timing")
async def get_send_timing(db: AsyncSession = Depends(get_db)):
    """Get optimal email send timing analysis."""
    from app.services.send_timing import get_send_timing as get_timing
    analyzer = get_timing()
    return await analyzer.get_optimal_times(db)
