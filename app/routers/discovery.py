"""Company discovery and enrichment endpoints.

Two routers live here, on deliberately different prefixes:

* ``router`` (``/discovery``) — the original Apollo-backed company search,
  website scrape and send-stats endpoints.
* ``places_router`` (``/dialtone/discovery``) — P4's Google Places SMB
  discovery: the Console-facing search / results / import / stats surface.
  Prefixed ``/dialtone`` to sit with the rest of the Console (the same
  precedent ``routers/verification.py`` set), and kept in this module because
  it is the same capability from an operator's point of view — "find me
  companies I don't have yet".

**Both must be registered in main.py.** A second router object in one module is
easy to add and easy to forget to wire up.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func as sqla_func, select

from app.database import get_db
from app.dependencies import OrgScope, get_org_scope
from app.models.company import Company
from app.models.discovered_business import DiscoveredBusiness
from app.models.discovery_search import DiscoverySearch
from app.models.prospecting import IcpProfile
from app.rate_limit import limiter
from app.schemas.discovery import (
    DiscoveredBusinessOut,
    DiscoveryResultsPage,
    DiscoverySearchCreate,
    DiscoverySearchOut,
    DiscoverySearchRequest,
    DiscoverySearchResponse,
    DiscoverySearchUpdate,
    DiscoveryStats,
    ImportBusinessRequest,
    ImportBusinessResponse,
)
from app.services import api_credits, gemini_discovery, org_gemini
from app.services.enrichment import get_enrichment_service
from app.services.ingestion import get_ingestion_service
from app.services.places_discovery import PROVIDER, get_places_service
from app.services.sql_search import LIKE_ESCAPE, contains_pattern

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/discovery", tags=["discovery"])
places_router = APIRouter(prefix="/dialtone/discovery", tags=["places-discovery"])

# Every Places call is billed, and an ad-hoc search can page three times.
# Throttled well below the endpoints that only read staged rows.
SEARCH_RATE_LIMIT = "10/minute"
IMPORT_RATE_LIMIT = "60/minute"


class CompanySearchRequest(BaseModel):
    # Free-text description of the companies wanted, e.g. "SaaS companies in
    # Bangalore focused on healthcare". Gemini search leads with it; Apollo
    # has no free-text search and ignores it.
    query: str = Field("", max_length=2000)
    industry: str = Field("", description="Industry keyword, e.g. 'injection molding'")
    location: str = Field("", description="Location, e.g. 'Ohio, United States'")
    keywords: list[str] = Field(default_factory=list, description="Additional keyword filters")
    min_employees: Optional[int] = None
    max_employees: Optional[int] = None
    per_page: int = Field(10, ge=1, le=25)
    # "gemini" runs the search on the org's own Gemini key with Google Search
    # grounding instead of Apollo — same response shape either way.
    source: Literal["apollo", "gemini"] = "apollo"


GEMINI_KEY_REQUIRED_MESSAGE = (
    "Add a Gemini API key in Settings → API Keys to search with Gemini "
    "(a Groq or OpenRouter key also works, without live web search)."
)


class EnrichCompanyRequest(BaseModel):
    find_contacts: bool = Field(True, description="Also search for contacts at this company")
    contact_titles: list[str] = Field(
        default_factory=list,
        description="Job titles to search for (empty = use defaults)"
    )


def _search_error_message(status: str, error: str | None) -> str | None:
    """Operator-facing reason for a search that did not complete."""
    if status == "ok":
        return None
    if status == "skipped" and error == "no_api_key":
        return "Apollo is not configured: no Apollo API key is set for this deployment."
    if status == "unauthorized":
        return (
            "Apollo refused the request: the API key was rejected or the Apollo "
            f"plan does not include company search. {error or ''}"
        ).strip()
    return error


def _company_row(outcome) -> dict:
    return {
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
    }


@router.post("/search-companies-gemini")
async def search_companies_gemini(
    payload: CompanySearchRequest,
    db: AsyncSession = Depends(get_db),
    scope: OrgScope = Depends(get_org_scope),
):
    """Search the web with the org's Gemini (Google Search grounding) and import.

    Same response shape as the Apollo search, so the Discovery tab renders it
    unchanged, plus ``source``, grounding ``sources`` and per-company
    ``description`` / ``location`` / ``contacts``. When Gemini can't serve, the
    org's Groq/OpenRouter key answers from training data: ``grounded`` is then
    False and ``notice`` explains it — show it with the results. Failures come back as a
    non-``ok`` ``search_status`` with an operator-readable ``search_error`` —
    exactly how the Apollo path reports a refused key — rather than as an HTTP
    error the tab would show as "something went wrong".
    """
    base = {
        "source": gemini_discovery.SOURCE,
        "total": 0,
        "imported": 0,
        "already_existed": 0,
        "search_status": "ok",
        "search_error": None,
        "search_error_code": None,
        "credits_spent": 0,
        "errors": [],
        "companies": [],
        "sources": [],
        # Which provider answered, and whether that answer is web-grounded.
        # Only Gemini searches the web; a Groq/OpenRouter fallback answers from
        # training data, and ``notice`` says so for the UI to show.
        "provider": None,
        "grounded": False,
        "notice": None,
    }
    if scope.org_id is None:
        raise HTTPException(status_code=400, detail="No organization context")
    try:
        found = await gemini_discovery.search_companies(
            db,
            scope.org_id,
            query=payload.query,
            industry=payload.industry,
            location=payload.location,
            keywords=payload.keywords,
            min_employees=payload.min_employees,
            max_employees=payload.max_employees,
            limit=payload.per_page,
        )
    except org_gemini.GeminiKeyMissing:
        return {
            **base,
            "search_status": "skipped",
            "search_error": GEMINI_KEY_REQUIRED_MESSAGE,
            "search_error_code": "gemini_key_required",
        }
    except org_gemini.GeminiError as exc:
        return {**base, "search_status": "error", "search_error": str(exc)}

    result = await get_ingestion_service().ingest(db, found.records)
    companies = []
    for outcome in result.outcomes:
        extra = found.extras.get(id(outcome.record), {})
        companies.append({
            **_company_row(outcome),
            "country": outcome.record.country,
            "website": outcome.record.website,
            "description": extra.get("description"),
            "location": extra.get("location"),
            "contacts": [
                {
                    "name": " ".join(p for p in (c.first_name, c.last_name) if p) or None,
                    "title": c.title,
                    "email": c.email,
                    "linkedin": c.linkedin_url,
                }
                for c in outcome.record.contacts
            ],
        })
    return {
        **base,
        "total": len(companies),
        "imported": result.companies_created,
        "already_existed": result.companies_matched,
        "errors": result.errors,
        "companies": companies,
        "sources": found.sources,
        "provider": found.provider,
        "grounded": found.grounded,
        "notice": found.notice,
    }


@router.post("/search-companies")
async def search_companies(
    payload: CompanySearchRequest,
    db: AsyncSession = Depends(get_db),
    scope: OrgScope = Depends(get_org_scope),
):
    """Search Apollo (or, with ``source="gemini"``, Gemini) and import.

    Two steps now, deliberately: Apollo returns records, the ingestion service
    persists them under the one shared dedup rule. ``get_db`` owns the commit —
    the service used to commit mid-request on its own.
    """
    if payload.source == "gemini":
        return await search_companies_gemini(payload, db, scope)
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

    companies = [_company_row(outcome) for outcome in result.outcomes]
    return {
        "source": "apollo",
        "total": len(companies),
        "imported": result.companies_created,
        "already_existed": result.companies_matched,
        # 'ok' | 'rate_limited' | 'budget_exhausted' | 'error' | 'skipped' |
        # 'unauthorized' —
        # a rate-limited search must not read as "found nothing".
        "search_status": search.status,
        # Why a non-'ok' search stopped, in words an operator can act on —
        # "Apollo not configured" must not surface as a bare failure.
        "search_error": _search_error_message(search.status, search.error),
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


# ── P4: Google Places SMB discovery ──────────────────────────────────────────


async def _load_business(db: AsyncSession, business_id: uuid.UUID) -> DiscoveredBusiness:
    business = (
        await db.execute(
            select(DiscoveredBusiness).where(DiscoveredBusiness.id == business_id)
        )
    ).scalar_one_or_none()
    if business is None:
        raise HTTPException(status_code=404, detail="Discovered business not found")
    return business


async def _load_search(db: AsyncSession, search_id: uuid.UUID) -> DiscoverySearch:
    search = (
        await db.execute(select(DiscoverySearch).where(DiscoverySearch.id == search_id))
    ).scalar_one_or_none()
    if search is None:
        raise HTTPException(status_code=404, detail="Discovery search not found")
    return search


@places_router.post("/search", response_model=DiscoverySearchResponse)
@limiter.limit(SEARCH_RATE_LIMIT)
async def run_discovery_search(
    request: Request,
    payload: DiscoverySearchRequest,
    db: AsyncSession = Depends(get_db),
):
    """Run a Places search now and stage what it finds.

    Returns 200 even when Places is unreachable, rate-limited or over budget:
    the response carries the real ``status`` and ``error``. A 5xx would say the
    *request* was bad when the truth is upstream — and a bare ``found: 0`` would
    say the market is empty when we simply never got to ask.

    ``save=false`` still spends credits (Google bills the call, not the row) but
    stages nothing — the preview case.
    """
    places = get_places_service()
    if not places.enabled:
        return DiscoverySearchResponse(
            status="skipped",
            found=0,
            created=0,
            duplicates=0,
            credits_spent=0,
            error="GOOGLE_PLACES_API_KEY is not configured",
        )

    if payload.save:
        # A transient search row: it gives every staged business the same
        # provenance a scheduled one has, so "which query found this?" is
        # answerable for ad-hoc searches too.
        search = DiscoverySearch(
            name=payload.query[:255],
            query=payload.query,
            location_text=payload.location,
            radius_km=int(payload.radius_km) if payload.radius_km else None,
            lat=payload.lat,
            lng=payload.lng,
            business_type=payload.business_type,
            country=payload.country,
            schedule="one_time",
            is_active=False,  # already run; the sweep must not run it again
        )
        db.add(search)
        await db.flush()
        outcome = await get_places_service().discover(db, search, limit=payload.limit)
        search.last_run_at = datetime.now(timezone.utc)
        search.last_status = outcome.status
        search.last_error = outcome.error
        search.results_count = outcome.created
        search.credits_spent = outcome.credits_spent
        await db.flush()
        return DiscoverySearchResponse(
            status=outcome.status,
            found=len(outcome.businesses),
            created=outcome.created,
            duplicates=outcome.duplicates,
            credits_spent=outcome.credits_spent,
            truncated=outcome.truncated,
            error=outcome.error,
            search_id=search.id,
            results=[DiscoveredBusinessOut.model_validate(b) for b in outcome.businesses],
        )

    found = await places.search_businesses(
        db,
        payload.query,
        location=payload.location,
        radius_km=payload.radius_km,
        lat=payload.lat,
        lng=payload.lng,
        business_type=payload.business_type,
        limit=payload.limit,
        country=payload.country,
    )
    # Preview: echo the normalized places without persisting them. Shaped like
    # a staged row so the Console renders one list, not two.
    previews = [
        DiscoveredBusinessOut(
            id=uuid.uuid4(),
            place_id=p.place_id,
            business_name=p.name,
            address=p.address,
            city=p.city,
            state=p.state,
            country=p.country,
            phone=p.phone,
            website=p.website,
            domain=p.domain,
            category=p.category,
            rating=p.rating,
            review_count=p.review_count,
            status="new",
            discovered_via=PROVIDER,
            search_query=payload.query,
            created_at=datetime.now(timezone.utc),
        )
        for p in found.results
    ]
    return DiscoverySearchResponse(
        status=found.status,
        found=len(found.results),
        created=0,
        duplicates=0,
        credits_spent=found.credits_spent,
        truncated=found.truncated,
        error=found.error,
        results=previews,
    )


@places_router.get("/results", response_model=DiscoveryResultsPage)
async def list_discovery_results(
    status: Optional[str] = Query(None, description="new | enriched | imported | skipped"),
    country: Optional[str] = Query(None, max_length=2),
    category: Optional[str] = None,
    search_id: Optional[uuid.UUID] = None,
    has_email: Optional[bool] = Query(None, description="Only businesses with/without an email"),
    has_website: Optional[bool] = None,
    q: Optional[str] = Query(None, description="Match against business name"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """List staged businesses, newest first."""
    filters = []
    if status:
        filters.append(DiscoveredBusiness.status == status)
    if country:
        filters.append(DiscoveredBusiness.country == country.upper())
    if category:
        filters.append(DiscoveredBusiness.category == category)
    if search_id:
        filters.append(DiscoveredBusiness.search_id == search_id)
    if has_email is not None:
        filters.append(
            DiscoveredBusiness.email.isnot(None)
            if has_email
            else DiscoveredBusiness.email.is_(None)
        )
    if has_website is not None:
        filters.append(
            DiscoveredBusiness.domain.isnot(None)
            if has_website
            else DiscoveredBusiness.domain.is_(None)
        )
    if q:
        filters.append(
            DiscoveredBusiness.business_name.ilike(
                contains_pattern(q), escape=LIKE_ESCAPE
            )
        )

    total = (
        await db.execute(
            select(sqla_func.count(DiscoveredBusiness.id)).where(*filters)
        )
    ).scalar() or 0
    rows = (
        await db.execute(
            select(DiscoveredBusiness)
            .where(*filters)
            .order_by(DiscoveredBusiness.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return DiscoveryResultsPage(
        total=int(total),
        limit=limit,
        offset=offset,
        results=[DiscoveredBusinessOut.model_validate(r) for r in rows],
    )


@places_router.post("/results/{business_id}/import", response_model=ImportBusinessResponse)
@limiter.limit(IMPORT_RATE_LIMIT)
async def import_discovered_business(
    request: Request,
    business_id: uuid.UUID,
    payload: ImportBusinessRequest,
    db: AsyncSession = Depends(get_db),
):
    """Import a staged business as a company (and a contact, if it has an email).

    Goes through the one shared ``IngestionService`` — same dedup rule, same
    suppression rules, same scoring as Apollo and CSV. A business with no email
    still imports: it becomes a company with a phone number, which the call
    queue can use. It just doesn't get a contact, because Places gave us nobody
    to make one out of.
    """
    business = await _load_business(db, business_id)

    if business.company_id is not None:
        # Idempotent: a second import must not create a second company.
        return ImportBusinessResponse(
            status="already_imported",
            business=DiscoveredBusinessOut.model_validate(business),
            company_id=business.company_id,
            contact_id=business.contact_id,
        )

    enrichment: Optional[dict] = None
    if payload.enrich and not business.email:
        enrichment = await get_places_service().extract_email(db, business)

    icp = None
    if payload.icp_profile_id:
        icp = (
            await db.execute(
                select(IcpProfile).where(IcpProfile.id == payload.icp_profile_id)
            )
        ).scalar_one_or_none()
        if icp is None:
            raise HTTPException(status_code=404, detail="ICP profile not found")

    outcome = await get_places_service().import_business(db, business, icp=icp)

    if outcome.error or outcome.company is None:
        # The common real cause is a country outside settings.supported_countries.
        # Recorded on the row rather than raised: the business is still a valid
        # discovery, it just isn't importable under current market policy.
        business.status = "skipped"
        business.status_reason = outcome.error or f"not imported (route={outcome.route})"
        await db.flush()
        return ImportBusinessResponse(
            status="skipped",
            business=DiscoveredBusinessOut.model_validate(business),
            route=outcome.route,
            dedup_confidence=outcome.dedup.confidence,
            email_enrichment=enrichment,
            error=outcome.error,
        )

    business.company_id = outcome.company.id
    business.status = "imported"
    business.imported_at = datetime.now(timezone.utc)
    # The contact the ingestion service just created for this email, if any.
    if business.email:
        contact = await _find_imported_contact(db, outcome.company.id, business.email)
        business.contact_id = contact.id if contact else None
    await db.flush()

    return ImportBusinessResponse(
        status="imported",
        business=DiscoveredBusinessOut.model_validate(business),
        company_id=outcome.company.id,
        contact_id=business.contact_id,
        created_company=outcome.created,
        contacts_created=outcome.contacts_created,
        dedup_confidence=outcome.dedup.confidence,
        dedup_matched_on=outcome.dedup.matched_on,
        route=outcome.route,
        fit_score=outcome.fit_score,
        email_enrichment=enrichment,
    )


async def _find_imported_contact(db: AsyncSession, company_id: uuid.UUID, email: str):
    from app.models.contact import Contact

    return (
        await db.execute(
            select(Contact)
            .where(sqla_func.lower(Contact.email) == email.strip().lower())
            .limit(1)
        )
    ).scalar_one_or_none()


@places_router.post("/searches", response_model=DiscoverySearchOut, status_code=201)
async def create_discovery_search(
    payload: DiscoverySearchCreate,
    db: AsyncSession = Depends(get_db),
):
    """Save a search config. Does not run it — the beat sweep or ``/run`` does."""
    search = DiscoverySearch(**payload.model_dump())
    db.add(search)
    await db.flush()
    return DiscoverySearchOut.model_validate(search)


@places_router.get("/searches", response_model=list[DiscoverySearchOut])
async def list_discovery_searches(
    is_active: Optional[bool] = None,
    schedule: Optional[str] = Query(None, description="one_time | daily | weekly"),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """List saved searches, newest first."""
    filters = []
    if is_active is not None:
        filters.append(DiscoverySearch.is_active.is_(is_active))
    if schedule:
        filters.append(DiscoverySearch.schedule == schedule)
    rows = (
        await db.execute(
            select(DiscoverySearch)
            .where(*filters)
            .order_by(DiscoverySearch.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [DiscoverySearchOut.model_validate(r) for r in rows]


@places_router.patch("/searches/{search_id}", response_model=DiscoverySearchOut)
async def update_discovery_search(
    search_id: uuid.UUID,
    payload: DiscoverySearchUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update a saved search — retarget it, or switch it off."""
    search = await _load_search(db, search_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(search, field, value)
    await db.flush()
    return DiscoverySearchOut.model_validate(search)


@places_router.delete("/searches/{search_id}", status_code=204)
async def delete_discovery_search(
    search_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Deactivate a saved search.

    Soft: the businesses it found keep pointing at it, and deleting the row
    would erase the provenance behind every company it produced.
    """
    search = await _load_search(db, search_id)
    search.is_active = False
    await db.flush()
    return None


@places_router.post("/searches/{search_id}/run", response_model=DiscoverySearchResponse)
@limiter.limit(SEARCH_RATE_LIMIT)
async def run_saved_search(
    request: Request,
    search_id: uuid.UUID,
    limit: int = Query(60, ge=1, le=60),
    db: AsyncSession = Depends(get_db),
):
    """Run a saved search now, off-schedule."""
    search = await _load_search(db, search_id)
    places = get_places_service()
    if not places.enabled:
        return DiscoverySearchResponse(
            status="skipped",
            found=0,
            created=0,
            duplicates=0,
            credits_spent=0,
            search_id=search.id,
            error="GOOGLE_PLACES_API_KEY is not configured",
        )

    outcome = await places.discover(db, search, limit=limit)
    search.last_run_at = datetime.now(timezone.utc)
    search.last_status = outcome.status
    search.last_error = outcome.error
    search.results_count = (search.results_count or 0) + outcome.created
    search.credits_spent = (search.credits_spent or 0) + outcome.credits_spent
    await db.flush()
    return DiscoverySearchResponse(
        status=outcome.status,
        found=len(outcome.businesses),
        created=outcome.created,
        duplicates=outcome.duplicates,
        credits_spent=outcome.credits_spent,
        truncated=outcome.truncated,
        error=outcome.error,
        search_id=search.id,
        results=[DiscoveredBusinessOut.model_validate(b) for b in outcome.businesses],
    )


@places_router.get("/stats", response_model=DiscoveryStats)
async def get_discovery_stats(db: AsyncSession = Depends(get_db)):
    """The discovery funnel.

    The ratios are the point, not the totals. Places -> website -> email ->
    import loses volume at every hop, and ``email_yield`` is the number that
    says whether this capability is earning its credits or just staging rows.
    """
    total = int(
        (await db.execute(select(sqla_func.count(DiscoveredBusiness.id)))).scalar() or 0
    )
    status_rows = (
        await db.execute(
            select(DiscoveredBusiness.status, sqla_func.count(DiscoveredBusiness.id))
            .group_by(DiscoveredBusiness.status)
        )
    ).all()
    source_rows = (
        await db.execute(
            select(
                DiscoveredBusiness.email_source,
                sqla_func.count(DiscoveredBusiness.id),
            )
            .where(DiscoveredBusiness.email_source.isnot(None))
            .group_by(DiscoveredBusiness.email_source)
        )
    ).all()
    with_website = int(
        (
            await db.execute(
                select(sqla_func.count(DiscoveredBusiness.id)).where(
                    DiscoveredBusiness.domain.isnot(None)
                )
            )
        ).scalar() or 0
    )
    with_email = int(
        (
            await db.execute(
                select(sqla_func.count(DiscoveredBusiness.id)).where(
                    DiscoveredBusiness.email.isnot(None)
                )
            )
        ).scalar() or 0
    )
    imported = int(
        (
            await db.execute(
                select(sqla_func.count(DiscoveredBusiness.id)).where(
                    DiscoveredBusiness.status == "imported"
                )
            )
        ).scalar() or 0
    )
    searches_active = int(
        (
            await db.execute(
                select(sqla_func.count(DiscoverySearch.id)).where(
                    DiscoverySearch.is_active.is_(True)
                )
            )
        ).scalar() or 0
    )
    budget = await api_credits.get_budget_status(db, PROVIDER, cost=0)

    return DiscoveryStats(
        total=total,
        by_status={s: int(c) for s, c in status_rows},
        with_website=with_website,
        with_email=with_email,
        imported=imported,
        by_email_source={s: int(c) for s, c in source_rows},
        # Guarded: an empty table is 0%, not a crash.
        email_yield=round(with_email / total, 4) if total else 0.0,
        import_yield=round(imported / total, 4) if total else 0.0,
        searches_active=searches_active,
        credits_spent_today=budget.daily_spent,
    )
