"""Pydantic schemas for deals."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

VALID_DEAL_STATUSES = ("won", "lost", "pending")
VALID_DEAL_STAGES = ("qualification", "discovery", "proposal", "negotiation", "closing")
DEAL_STAGE_PROBABILITIES = {
    "qualification": 10,
    "discovery": 25,
    "proposal": 50,
    "negotiation": 75,
    "closing": 90,
}


class DealCreate(BaseModel):
    company_id: uuid.UUID
    title: str = Field(..., max_length=255)
    value: Optional[float] = Field(None, ge=0)
    currency: str = Field("USD", min_length=3, max_length=3)
    close_date: Optional[datetime] = None
    status: str = "pending"
    deal_stage: str = "qualification"
    lost_reason: Optional[str] = Field(None, max_length=255)
    notes: Optional[str] = None

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v):
        if v not in VALID_DEAL_STATUSES:
            raise ValueError(f"status must be one of {VALID_DEAL_STATUSES}")
        return v

    @field_validator("deal_stage")
    @classmethod
    def _validate_deal_stage(cls, v):
        if v not in VALID_DEAL_STAGES:
            raise ValueError(f"deal_stage must be one of {VALID_DEAL_STAGES}")
        return v

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, v):
        return v.upper()


class DealUpdate(BaseModel):
    title: Optional[str] = Field(None, max_length=255)
    value: Optional[float] = Field(None, ge=0)
    currency: Optional[str] = Field(None, min_length=3, max_length=3)
    close_date: Optional[datetime] = None
    status: Optional[str] = None
    deal_stage: Optional[str] = None
    lost_reason: Optional[str] = Field(None, max_length=255)
    notes: Optional[str] = None

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v):
        if v is not None and v not in VALID_DEAL_STATUSES:
            raise ValueError(f"status must be one of {VALID_DEAL_STATUSES}")
        return v

    @field_validator("deal_stage")
    @classmethod
    def _validate_deal_stage(cls, v):
        if v is not None and v not in VALID_DEAL_STAGES:
            raise ValueError(f"deal_stage must be one of {VALID_DEAL_STAGES}")
        return v

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, v):
        return v.upper() if v else v


class DealRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    title: str
    value: Optional[float] = None
    currency: str
    close_date: Optional[datetime] = None
    status: str
    deal_stage: str
    lost_reason: Optional[str] = None
    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class DealStatusBreakdown(BaseModel):
    status: str
    count: int
    total_value: float


class DealSummary(BaseModel):
    total_deals: int
    won_count: int
    lost_count: int
    pending_count: int
    won_revenue: float
    pipeline_value: float  # sum of pending deal values
    lost_value: float
    win_rate: float  # won / (won + lost), percentage
    by_status: list[DealStatusBreakdown]


class DealActivityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    deal_id: uuid.UUID
    activity_type: str
    description: Optional[str] = None
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    created_at: datetime


class DealNoteCreate(BaseModel):
    content: str


class DealNoteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    deal_id: uuid.UUID
    content: str
    created_at: datetime
    updated_at: datetime


class DealStageBreakdown(BaseModel):
    stage: str
    count: int
    value: float


class DealMetrics(BaseModel):
    total_pipeline_value: float
    weighted_pipeline_value: float
    average_deal_size: float
    deal_count: int
    win_rate: float
    avg_days_to_close: float
    by_stage: list[DealStageBreakdown]
