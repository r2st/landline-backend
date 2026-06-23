"""Pydantic schemas for deals."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

VALID_DEAL_STATUSES = ("won", "lost", "pending")


class DealCreate(BaseModel):
    company_id: uuid.UUID
    title: str = Field(..., max_length=255)
    value: Optional[float] = Field(None, ge=0)
    currency: str = Field("USD", min_length=3, max_length=3)
    close_date: Optional[datetime] = None
    status: str = "pending"
    lost_reason: Optional[str] = Field(None, max_length=255)
    notes: Optional[str] = None

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v):
        if v not in VALID_DEAL_STATUSES:
            raise ValueError(f"status must be one of {VALID_DEAL_STATUSES}")
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
    lost_reason: Optional[str] = Field(None, max_length=255)
    notes: Optional[str] = None

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v):
        if v is not None and v not in VALID_DEAL_STATUSES:
            raise ValueError(f"status must be one of {VALID_DEAL_STATUSES}")
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
