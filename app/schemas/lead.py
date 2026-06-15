"""Pydantic schemas for lead scoring."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class LeadScoreRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    score_type: str
    fit_score: int
    industry_score: int
    size_score: int
    location_score: int
    engagement_score: int
    factors: dict
    scored_at: datetime


class LeadScoreRequest(BaseModel):
    company_id: uuid.UUID


class LeadScoreBatchRequest(BaseModel):
    company_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=500)


class LeadScoreBatchResponse(BaseModel):
    scored: int
    errors: list[dict]


class LeadPipelineItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    company_id: uuid.UUID
    company_name: str
    domain: Optional[str] = None
    stage: str
    fit_score: Optional[int] = None
    country: str
    employee_count: Optional[int] = None
    industry_segment: Optional[str] = None


class LeadPipelineResponse(BaseModel):
    items: list[LeadPipelineItem]
    total: int
    page: int
    page_size: int


class ConsentCreate(BaseModel):
    contact_id: uuid.UUID
    consent_type: str = Field(..., pattern="^(express|implied|none)$")
    basis: Optional[str] = None
    evidence_url: Optional[str] = None
    granted_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


class ConsentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    contact_id: uuid.UUID
    consent_type: str
    basis: Optional[str] = None
    evidence_url: Optional[str] = None
    granted_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    created_at: datetime
