"""Pydantic schemas for Google Places SMB discovery (P4)."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.constants import COUNTRY_QUERY_PATTERN
from app.models.discovered_business import DISCOVERY_STATUSES
from app.models.discovery_search import DISCOVERY_SCHEDULES

# Literal-ish validation without importing Literal for every tuple: the model
# module owns the vocabulary, the schema reuses it, so they can't drift.
_SCHEDULE_PATTERN = f"^({'|'.join(DISCOVERY_SCHEDULES)})$"
_STATUS_PATTERN = f"^({'|'.join(DISCOVERY_STATUSES)})$"


class DiscoverySearchRequest(BaseModel):
    """An ad-hoc search — run now, save nothing."""

    query: str = Field(..., min_length=2, max_length=500, description="e.g. 'injection molding manufacturers'")
    location: Optional[str] = Field(None, max_length=255, description="e.g. 'Pune, India'")
    radius_km: Optional[float] = Field(None, gt=0, le=50, description="Only applied with lat/lng")
    lat: Optional[float] = Field(None, ge=-90, le=90)
    lng: Optional[float] = Field(None, ge=-180, le=180)
    business_type: Optional[str] = Field(None, max_length=100, description="Places includedType")
    # The schema allowlist, not the policy one: a country we can stage but not
    # yet import is a legitimate thing to search. Import enforces the policy.
    country: Optional[str] = Field(None, pattern=COUNTRY_QUERY_PATTERN)
    # Places' own hard ceiling is 60 per query; the router clamps to it.
    limit: int = Field(20, ge=1, le=60)
    # Persist what we find. False = preview: costs credits, stages nothing.
    save: bool = True


class DiscoverySearchCreate(BaseModel):
    """A saved, optionally recurring search config."""

    name: str = Field(..., min_length=1, max_length=255)
    query: str = Field(..., min_length=2, max_length=500)
    location_text: Optional[str] = Field(None, max_length=255)
    radius_km: Optional[int] = Field(None, gt=0, le=50)
    lat: Optional[float] = Field(None, ge=-90, le=90)
    lng: Optional[float] = Field(None, ge=-180, le=180)
    business_type: Optional[str] = Field(None, max_length=100)
    country: Optional[str] = Field(None, pattern=COUNTRY_QUERY_PATTERN)
    schedule: str = Field("one_time", pattern=_SCHEDULE_PATTERN)
    is_active: bool = True


class DiscoverySearchUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    query: Optional[str] = Field(None, min_length=2, max_length=500)
    location_text: Optional[str] = Field(None, max_length=255)
    radius_km: Optional[int] = Field(None, gt=0, le=50)
    business_type: Optional[str] = Field(None, max_length=100)
    country: Optional[str] = Field(None, pattern=COUNTRY_QUERY_PATTERN)
    schedule: Optional[str] = Field(None, pattern=_SCHEDULE_PATTERN)
    is_active: Optional[bool] = None


class DiscoverySearchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    query: str
    location_text: Optional[str] = None
    radius_km: Optional[int] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    business_type: Optional[str] = None
    country: Optional[str] = None
    schedule: str
    is_active: bool
    last_run_at: Optional[datetime] = None
    results_count: int
    last_status: Optional[str] = None
    last_error: Optional[str] = None
    credits_spent: int
    created_at: datetime


class DiscoveredBusinessOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    place_id: str
    business_name: str
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None
    domain: Optional[str] = None
    email: Optional[str] = None
    email_source: Optional[str] = None
    email_confidence: Optional[int] = None
    category: Optional[str] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    status: str
    status_reason: Optional[str] = None
    discovered_via: str
    search_query: Optional[str] = None
    search_id: Optional[uuid.UUID] = None
    company_id: Optional[uuid.UUID] = None
    contact_id: Optional[uuid.UUID] = None
    discovered_at: Optional[datetime] = None
    imported_at: Optional[datetime] = None
    created_at: datetime


class DiscoverySearchResponse(BaseModel):
    """The result of triggering a search.

    ``status`` is deliberately surfaced rather than folded into a bare count:
    ``rate_limited`` and ``budget_exhausted`` must never read as "found
    nothing", which is exactly what a lone ``found: 0`` would say.
    """

    status: str
    found: int
    created: int
    duplicates: int
    credits_spent: int
    truncated: bool = False
    error: Optional[str] = None
    search_id: Optional[uuid.UUID] = None
    results: list[DiscoveredBusinessOut] = Field(default_factory=list)


class DiscoveryResultsPage(BaseModel):
    total: int
    limit: int
    offset: int
    results: list[DiscoveredBusinessOut] = Field(default_factory=list)


class ImportBusinessRequest(BaseModel):
    # Try Hunter/scrape for an email first when the business has none. Off by
    # default: it spends a Hunter credit and blocks the request on a scrape.
    enrich: bool = False
    # Score against an ICP, which also enables fit-based routing.
    icp_profile_id: Optional[uuid.UUID] = None


class ImportBusinessResponse(BaseModel):
    status: str
    business: DiscoveredBusinessOut
    company_id: Optional[uuid.UUID] = None
    contact_id: Optional[uuid.UUID] = None
    created_company: bool = False
    contacts_created: int = 0
    dedup_confidence: Optional[str] = None
    dedup_matched_on: Optional[str] = None
    route: Optional[str] = None
    fit_score: Optional[int] = None
    email_enrichment: Optional[dict] = None
    error: Optional[str] = None


class DiscoveryStats(BaseModel):
    """The funnel, which for this capability is the whole point.

    Places -> emailable contact loses volume at every hop, so the counts that
    matter are the ratios between these, not any one of them.
    """

    total: int
    by_status: dict[str, int] = Field(default_factory=dict)
    with_website: int
    with_email: int
    imported: int
    by_email_source: dict[str, int] = Field(default_factory=dict)
    # The honest headline: what fraction of discovered businesses ever reached
    # an email address at all.
    email_yield: float
    import_yield: float
    searches_active: int
    credits_spent_today: int
