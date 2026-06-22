"""Pydantic schemas for Company endpoints."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.normalize import normalize_domain

VALID_STAGES = (
    "new", "researched", "scored", "contacted", "engaged",
    "qualified", "opportunity", "customer", "disqualified",
)
VALID_COUNTRIES = ("US", "CA")


class CompanyBase(BaseModel):
    name: str = Field(..., max_length=255)
    domain: Optional[str] = Field(None, max_length=255)
    city: Optional[str] = Field(None, max_length=100)
    state_province: Optional[str] = Field(None, max_length=100)
    country: str = Field("US", pattern="^(US|CA)$")
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
    country: Optional[str] = Field(None, pattern="^(US|CA)$")
    employee_count: Optional[int] = Field(None, ge=0)
    naics_code: Optional[str] = Field(None, max_length=10)
    naics_description: Optional[str] = Field(None, max_length=255)
    industry_segment: Optional[str] = Field(None, max_length=100)
    website: Optional[str] = None
    phone: Optional[str] = Field(None, max_length=30)
    stage: Optional[str] = None
    tags: Optional[list[str]] = None
    notes: Optional[str] = None
    source: Optional[str] = Field(None, max_length=50)
    source_url: Optional[str] = None

    @field_validator("domain", mode="before")
    @classmethod
    def _normalize_domain(cls, v):
        return normalize_domain(v) if v else v


class CompanyStageUpdate(BaseModel):
    stage: str

    def model_post_init(self, __context):
        if self.stage not in VALID_STAGES:
            raise ValueError(f"stage must be one of {VALID_STAGES}")


class CompanyRead(CompanyBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source: str
    source_url: Optional[str] = None
    stage: str
    created_at: datetime
    updated_at: datetime
    # Derived, attached by the router (not stored on the model itself).
    fit_score: Optional[int] = None
    contact_count: Optional[int] = None


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
