"""Pydantic schemas for lead prioritization & routing (Feature 1.3)."""

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class RoutingDetail(BaseModel):
    """Propensity + rep-match detail for one company."""

    company_id: uuid.UUID
    company_name: str
    fit_score: int
    propensity_score: int
    signal_velocity: float
    last_signal_at: Optional[datetime] = None
    recommended_rep_id: Optional[uuid.UUID] = None
    recommended_rep_name: Optional[str] = None
    pipeline_stage: str
    factors: dict[str, Any] = {}
    scored_at: datetime


class PrioritizedLead(BaseModel):
    company_id: uuid.UUID
    company_name: str
    fit_score: int
    propensity_score: int
    signal_velocity: float
    priority_score: float  # blended fit + propensity used for ranking
    last_signal_at: Optional[datetime] = None
    recommended_rep_id: Optional[uuid.UUID] = None
    recommended_rep_name: Optional[str] = None
    pipeline_stage: str


class PrioritizedLeadsResponse(BaseModel):
    leads: list[PrioritizedLead]
    total: int


class RepProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    user_name: Optional[str] = None
    industries: list[str] = []
    win_rate_by_segment: dict[str, Any] = {}
    total_won: int
    total_lost: int
    overall_win_rate: float
    avg_deal_velocity_days: Optional[float] = None
    max_capacity: int
    active_load: int
    updated_at: datetime


class RepProfileUpsert(BaseModel):
    industries: Optional[list[str]] = None
    max_capacity: Optional[int] = Field(None, ge=0, le=1000)
