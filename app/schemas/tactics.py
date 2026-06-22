"""Pydantic schemas for the sales-tactics (playbook) endpoints.

These power the frontend "Tactics" tab — multi-channel outreach assets generated
by Claude for a single lead. Request models validate the inputs the frontend
sends; the AI-generated payloads are returned as free-form dicts so the prompt
can evolve without lock-step schema changes.
"""

from typing import Optional

from pydantic import BaseModel, Field


class LinkedinDraftRequest(BaseModel):
    company_name: str = Field(..., max_length=255)
    contact_name: str = Field(..., max_length=255)
    contact_title: str = Field("", max_length=255)
    company_industry: str = Field("", max_length=255)
    company_domain: str = Field("", max_length=255)
    topic: str = Field("", max_length=500)


class PhoneScriptRequest(BaseModel):
    company_name: str = Field(..., max_length=255)
    contact_name: str = Field(..., max_length=255)
    contact_title: str = Field("", max_length=255)
    company_industry: str = Field("", max_length=255)
    product: str = Field("", max_length=255)


class ReferralDraftRequest(BaseModel):
    company_name: str = Field(..., max_length=255)
    referrer_name: str = Field(..., max_length=255)
    target_name: str = Field(..., max_length=255)
    target_title: str = Field("", max_length=255)
    company_industry: str = Field("", max_length=255)


class EventOutreachRequest(BaseModel):
    company_name: str = Field(..., max_length=255)
    contact_name: str = Field(..., max_length=255)
    event_name: str = Field(..., max_length=255)
    event_type: str = Field("conference", max_length=50)
    company_industry: str = Field("", max_length=255)


class DiscoverExecutivesResponse(BaseModel):
    company_id: str
    company_name: str
    executives: list[dict]
    executives_found: int
    contacts_created: int
    message: Optional[str] = None
