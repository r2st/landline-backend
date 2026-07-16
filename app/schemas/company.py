"""Pydantic schemas for Company endpoints."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.constants import ALLOWED_COUNTRIES, COUNTRY_QUERY_PATTERN
from app.services.normalize import normalize_domain

VALID_STAGES = (
    "new", "researched", "scored", "contacted", "engaged",
    "qualified", "opportunity", "customer", "disqualified",
)
# Kanban deal-board stages (distinct from the lifecycle ``stage`` above).
VALID_PIPELINE_STAGES = (
    "new_lead", "researching", "outreach", "engaged",
    "negotiating", "client", "churned",
)
# The schema-level allowlist, shared with the companies.country CHECK
# constraint so the API and the database can never disagree about what is
# insertable.
VALID_COUNTRIES = ALLOWED_COUNTRIES
_COUNTRY_PATTERN = COUNTRY_QUERY_PATTERN


class CompanyBase(BaseModel):
    name: str = Field(..., max_length=255)
    domain: Optional[str] = Field(None, max_length=255)
    city: Optional[str] = Field(None, max_length=100)
    state_province: Optional[str] = Field(None, max_length=100)
    country: str = Field("US", pattern=_COUNTRY_PATTERN)
    employee_count: Optional[int] = Field(None, ge=0)
    naics_code: Optional[str] = Field(None, max_length=10)
    naics_description: Optional[str] = Field(None, max_length=255)
    industry_segment: Optional[str] = Field(None, max_length=100)
    website: Optional[str] = None
    phone: Optional[str] = Field(None, max_length=30)
    tags: list[str] = Field(default_factory=list)
    notes: Optional[str] = None

    @field_validator("domain", mode="before")
    @classmethod
    def _normalize_domain(cls, v):
        # Store a bare lowercase host so domain-based dedup is reliable.
        return normalize_domain(v) if v else v


class CompanyCreate(CompanyBase):
    source: str = Field(..., max_length=50)
    source_url: Optional[str] = None


class CompanyUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=255)
    domain: Optional[str] = Field(None, max_length=255)
    city: Optional[str] = Field(None, max_length=100)
    state_province: Optional[str] = Field(None, max_length=100)
    country: Optional[str] = Field(None, pattern=_COUNTRY_PATTERN)
    employee_count: Optional[int] = Field(None, ge=0)
    naics_code: Optional[str] = Field(None, max_length=10)
    naics_description: Optional[str] = Field(None, max_length=255)
    industry_segment: Optional[str] = Field(None, max_length=100)
    website: Optional[str] = None
    phone: Optional[str] = Field(None, max_length=30)
    stage: Optional[str] = None
    pipeline_stage: Optional[str] = None
    tags: Optional[list[str]] = None
    notes: Optional[str] = None
    source: Optional[str] = Field(None, max_length=50)
    source_url: Optional[str] = None

    @field_validator("domain", mode="before")
    @classmethod
    def _normalize_domain(cls, v):
        return normalize_domain(v) if v else v

    @field_validator("pipeline_stage")
    @classmethod
    def _validate_pipeline_stage(cls, v):
        if v is not None and v not in VALID_PIPELINE_STAGES:
            raise ValueError(f"pipeline_stage must be one of {VALID_PIPELINE_STAGES}")
        return v


class CompanyStageUpdate(BaseModel):
    stage: str

    def model_post_init(self, __context):
        if self.stage not in VALID_STAGES:
            raise ValueError(f"stage must be one of {VALID_STAGES}")


class CompanyPipelineStageUpdate(BaseModel):
    pipeline_stage: str

    @field_validator("pipeline_stage")
    @classmethod
    def _validate(cls, v):
        if v not in VALID_PIPELINE_STAGES:
            raise ValueError(f"pipeline_stage must be one of {VALID_PIPELINE_STAGES}")
        return v


class BulkUpdateStageRequest(BaseModel):
    company_ids: list[uuid.UUID] = Field(..., min_length=1)
    pipeline_stage: str

    @field_validator("pipeline_stage")
    @classmethod
    def _validate(cls, v):
        if v not in VALID_PIPELINE_STAGES:
            raise ValueError(f"pipeline_stage must be one of {VALID_PIPELINE_STAGES}")
        return v


class BulkTagRequest(BaseModel):
    company_ids: list[uuid.UUID] = Field(..., min_length=1)
    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


class BulkOpResult(BaseModel):
    matched: int
    updated: int


class BulkExportRequest(BaseModel):
    # When omitted/empty, export every company.
    company_ids: Optional[list[uuid.UUID]] = None
    include_contacts: bool = True
    include_scores: bool = True
    include_activity: bool = False


class PipelineStageCount(BaseModel):
    stage: str
    count: int


class PipelineSummary(BaseModel):
    stages: list[PipelineStageCount]
    total: int


class CompanyContact(BaseModel):
    """A company's contact as shown alongside the company itself.

    Deliberately narrower than ``ContactRead``: this is the "who do we reach and
    how" summary a company list or detail header needs, not the full contact
    record (no card image, no postal address).
    """
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    # "First Last", or None when enrichment found an address but no name.
    # Deliberately not the Contact.full_name property, which reports "Unknown"
    # for a nameless contact — a literal "Unknown" rendered beside a real email
    # reads as a data error. Null lets the UI fall back to the address.
    full_name: Optional[str] = None
    title: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    mobile: Optional[str] = None
    linkedin_url: Optional[str] = None
    is_primary: bool = False
    # True when this contact can never be emailed again (opt-out/suppression),
    # so the UI can show the address without implying it is reachable.
    unsubscribed: bool = False
    suppressed: bool = False


class CompanyRead(CompanyBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source: str
    source_url: Optional[str] = None
    stage: str
    pipeline_stage: str
    created_at: datetime
    updated_at: datetime
    # Derived, attached by the router (not stored on the model itself).
    fit_score: Optional[int] = None
    contact_count: Optional[int] = None
    # The contact this company is represented by in a list — resolved by
    # app.services.company_contacts, not simply the ``is_primary`` row.
    primary_contact: Optional[CompanyContact] = None


class CompanyDetailRead(CompanyRead):
    """Single-company view — adds the full contact roster, best-first.

    Kept off ``CompanyRead`` so a 200-row list response can't drag every
    company's whole roster with it (and so the field name can't collide with
    the eagerly-loaded ``Company.contacts`` relationship during validation).
    """
    contacts: list[CompanyContact] = Field(default_factory=list)


class CompanyListResponse(BaseModel):
    items: list[CompanyRead]
    total: int
    page: int
    page_size: int


class CompanyMergeRequest(BaseModel):
    """Merge ``source_id`` (the duplicate) into the company in the path (survivor).

    All related records (contacts, scores, news, interactions, follow-ups, group
    memberships) are reassigned to the survivor, missing survivor fields are
    backfilled from the source, and the source company is deleted.
    """
    source_id: uuid.UUID


class CompanyMergeResponse(BaseModel):
    survivor_id: uuid.UUID
    merged_id: uuid.UUID
    contacts_moved: int
    scores_moved: int
    news_moved: int
    interactions_moved: int
    follow_ups_moved: int


class CompanySearchParams(BaseModel):
    q: Optional[str] = None
    country: Optional[str] = None
    stage: Optional[str] = None
    naics_code: Optional[str] = None
    industry_segment: Optional[str] = None
    min_employees: Optional[int] = None
    max_employees: Optional[int] = None
    tags: Optional[list[str]] = None
    page: int = Field(1, ge=1)
    page_size: int = Field(50, ge=1, le=200)
