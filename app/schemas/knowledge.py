"""Pydantic schemas for the knowledge base API."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# --- Industry contacts ---------------------------------------------------------

class IndustryContactBase(BaseModel):
    industry: str = Field(..., max_length=120)
    company_name: str = Field(..., max_length=255)
    website: Optional[str] = Field(None, max_length=500)
    location: Optional[str] = Field(None, max_length=255)
    employee_count: Optional[str] = Field(None, max_length=50)
    contact_name: Optional[str] = Field(None, max_length=255)
    contact_title: Optional[str] = Field(None, max_length=255)
    contact_email: Optional[str] = Field(None, max_length=320)
    notes: Optional[str] = None


class IndustryContactCreate(IndustryContactBase):
    pass


class IndustryContactUpdate(BaseModel):
    industry: Optional[str] = Field(None, max_length=120)
    company_name: Optional[str] = Field(None, max_length=255)
    website: Optional[str] = Field(None, max_length=500)
    location: Optional[str] = Field(None, max_length=255)
    employee_count: Optional[str] = Field(None, max_length=50)
    contact_name: Optional[str] = Field(None, max_length=255)
    contact_title: Optional[str] = Field(None, max_length=255)
    contact_email: Optional[str] = Field(None, max_length=320)
    notes: Optional[str] = None


class IndustryContactRead(IndustryContactBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    updated_at: datetime


class IndustryCount(BaseModel):
    """Distinct industry plus how many contacts it has — powers UI filters."""

    industry: str
    contact_count: int
    company_count: int


# --- Knowledge documents -------------------------------------------------------

class KnowledgeDocumentBase(BaseModel):
    key: str = Field(..., max_length=120, pattern=r"^[a-z0-9][a-z0-9-]*$")
    title: str = Field(..., max_length=255)
    category: str = Field("general", max_length=80)
    format: str = Field("markdown", max_length=20)
    content: str = ""


class KnowledgeDocumentCreate(KnowledgeDocumentBase):
    pass


class KnowledgeDocumentUpdate(BaseModel):
    title: Optional[str] = Field(None, max_length=255)
    category: Optional[str] = Field(None, max_length=80)
    format: Optional[str] = Field(None, max_length=20)
    content: Optional[str] = None


class KnowledgeDocumentSummary(BaseModel):
    """Document metadata without the (potentially large) body."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key: str
    title: str
    category: str
    format: str
    updated_at: datetime


class KnowledgeDocumentRead(KnowledgeDocumentBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    updated_at: datetime


# --- Industry verticals (read-only, sourced from the in-code registry) ---------

class SequenceStepRead(BaseModel):
    step_number: int
    delay_days: int
    subject_template: str
    body_template: str


class IndustryVerticalRead(BaseModel):
    key: str
    name: str
    sequence_name: str
    description: str
    demo_link: str
    keywords: list[str]
    pain_points: list[str]
    steps: list[SequenceStepRead]
    subject_ab: dict[int, list[str]]
