"""Pydantic schemas for the investor-outreach system."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.investor import (
    INVESTMENT_STAGES,
    INVESTOR_DOCUMENT_TYPES,
    INVESTOR_INTERACTION_DIRECTIONS,
    INVESTOR_INTERACTION_TYPES,
    INVESTOR_STATUSES,
)


def _check(value, allowed, label):
    if value is not None and value not in allowed:
        raise ValueError(f"{label} must be one of {allowed}")
    return value


# --------------------------------------------------------------------------- #
# Investors
# --------------------------------------------------------------------------- #


class InvestorCreate(BaseModel):
    name: str = Field(..., max_length=255)
    firm: Optional[str] = Field(None, max_length=255)
    email: Optional[str] = Field(None, max_length=320)
    phone: Optional[str] = Field(None, max_length=50)
    linkedin: Optional[str] = Field(None, max_length=500)
    website: Optional[str] = Field(None, max_length=500)
    investment_focus: list[str] = Field(default_factory=list)
    investment_stage: Optional[str] = None
    typical_check_size_min: Optional[int] = Field(None, ge=0)
    typical_check_size_max: Optional[int] = Field(None, ge=0)
    location: Optional[str] = Field(None, max_length=255)
    notes: Optional[str] = None
    status: str = "researching"
    source: Optional[str] = Field(None, max_length=100)

    @field_validator("status")
    @classmethod
    def _vs(cls, v):
        return _check(v, INVESTOR_STATUSES, "status")

    @field_validator("investment_stage")
    @classmethod
    def _vstage(cls, v):
        return _check(v, INVESTMENT_STAGES, "investment_stage")


class InvestorUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=255)
    firm: Optional[str] = Field(None, max_length=255)
    email: Optional[str] = Field(None, max_length=320)
    phone: Optional[str] = Field(None, max_length=50)
    linkedin: Optional[str] = Field(None, max_length=500)
    website: Optional[str] = Field(None, max_length=500)
    investment_focus: Optional[list[str]] = None
    investment_stage: Optional[str] = None
    typical_check_size_min: Optional[int] = Field(None, ge=0)
    typical_check_size_max: Optional[int] = Field(None, ge=0)
    location: Optional[str] = Field(None, max_length=255)
    notes: Optional[str] = None
    status: Optional[str] = None
    source: Optional[str] = Field(None, max_length=100)
    last_contacted_at: Optional[datetime] = None

    @field_validator("status")
    @classmethod
    def _vs(cls, v):
        return _check(v, INVESTOR_STATUSES, "status")

    @field_validator("investment_stage")
    @classmethod
    def _vstage(cls, v):
        return _check(v, INVESTMENT_STAGES, "investment_stage")


class InvestorRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    firm: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    linkedin: Optional[str] = None
    website: Optional[str] = None
    investment_focus: list[str] = Field(default_factory=list)
    investment_stage: Optional[str] = None
    typical_check_size_min: Optional[int] = None
    typical_check_size_max: Optional[int] = None
    location: Optional[str] = None
    notes: Optional[str] = None
    status: str
    source: Optional[str] = None
    last_contacted_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------- #
# Interactions
# --------------------------------------------------------------------------- #


class InvestorInteractionCreate(BaseModel):
    type: str
    direction: str = "outbound"
    subject: Optional[str] = Field(None, max_length=500)
    content: Optional[str] = None
    outcome: Optional[str] = None
    next_steps: Optional[str] = None
    interaction_date: Optional[datetime] = None

    @field_validator("type")
    @classmethod
    def _vt(cls, v):
        return _check(v, INVESTOR_INTERACTION_TYPES, "type")

    @field_validator("direction")
    @classmethod
    def _vd(cls, v):
        return _check(v, INVESTOR_INTERACTION_DIRECTIONS, "direction")


class InvestorInteractionUpdate(BaseModel):
    type: Optional[str] = None
    direction: Optional[str] = None
    subject: Optional[str] = Field(None, max_length=500)
    content: Optional[str] = None
    outcome: Optional[str] = None
    next_steps: Optional[str] = None
    interaction_date: Optional[datetime] = None

    @field_validator("type")
    @classmethod
    def _vt(cls, v):
        return _check(v, INVESTOR_INTERACTION_TYPES, "type")

    @field_validator("direction")
    @classmethod
    def _vd(cls, v):
        return _check(v, INVESTOR_INTERACTION_DIRECTIONS, "direction")


class InvestorInteractionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    investor_id: uuid.UUID
    type: str
    direction: str
    subject: Optional[str] = None
    content: Optional[str] = None
    outcome: Optional[str] = None
    next_steps: Optional[str] = None
    interaction_date: datetime
    created_at: datetime


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #


class InvestorDocumentCreate(BaseModel):
    investor_id: Optional[uuid.UUID] = None
    type: str = "other"
    title: str = Field(..., max_length=255)
    content: Optional[str] = None
    file_path: Optional[str] = Field(None, max_length=1000)
    version: int = Field(1, ge=1)

    @field_validator("type")
    @classmethod
    def _vt(cls, v):
        return _check(v, INVESTOR_DOCUMENT_TYPES, "type")


class InvestorDocumentUpdate(BaseModel):
    investor_id: Optional[uuid.UUID] = None
    type: Optional[str] = None
    title: Optional[str] = Field(None, max_length=255)
    content: Optional[str] = None
    file_path: Optional[str] = Field(None, max_length=1000)
    version: Optional[int] = Field(None, ge=1)

    @field_validator("type")
    @classmethod
    def _vt(cls, v):
        return _check(v, INVESTOR_DOCUMENT_TYPES, "type")


class InvestorDocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    investor_id: Optional[uuid.UUID] = None
    type: str
    title: str
    content: Optional[str] = None
    file_path: Optional[str] = None
    version: int
    created_at: datetime


# --------------------------------------------------------------------------- #
# Pipeline + timeline
# --------------------------------------------------------------------------- #


class InvestorPipelineStage(BaseModel):
    status: str
    count: int
    # Total of typical_check_size_max across investors in this stage (paise).
    potential_paise: int = 0


class InvestorPipeline(BaseModel):
    total: int
    stages: list[InvestorPipelineStage]


class InvestorTimelineEvent(BaseModel):
    id: uuid.UUID
    type: str
    direction: str
    subject: Optional[str] = None
    content: Optional[str] = None
    outcome: Optional[str] = None
    next_steps: Optional[str] = None
    interaction_date: datetime


class InvestorTimeline(BaseModel):
    investor: InvestorRead
    events: list[InvestorTimelineEvent]


# --------------------------------------------------------------------------- #
# AI outreach
# --------------------------------------------------------------------------- #


class InvestorOutreachRequest(BaseModel):
    # Optional extra colour about the raise / what to emphasise.
    custom_instructions: Optional[str] = None
    # Persist the generated draft as an outbound email interaction.
    log_interaction: bool = False


class InvestorOutreachResponse(BaseModel):
    investor_id: uuid.UUID
    investor_name: str
    firm: Optional[str] = None
    subject: str
    body: str
    # "ai" when Claude generated it, "template" on fallback (no key / error).
    source: str
    ai_available: bool = True
    note: Optional[str] = None
    interaction_id: Optional[uuid.UUID] = None
    generated_at: datetime
