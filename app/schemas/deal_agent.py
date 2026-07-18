"""Pydantic schemas for the Deal Progression Agent."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.deal_progression_agent import STAGE_ORDER


class AgentConfigRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    is_active: bool
    stale_thresholds: dict
    auto_advance_enabled: bool
    auto_followup_enabled: bool
    max_auto_followups_per_deal: int
    escalation_threshold_days: int
    created_at: datetime
    updated_at: datetime


class AgentConfigUpdate(BaseModel):
    """Partial update — every field optional so callers PATCH a subset."""

    is_active: Optional[bool] = None
    stale_thresholds: Optional[dict] = None
    auto_advance_enabled: Optional[bool] = None
    auto_followup_enabled: Optional[bool] = None
    max_auto_followups_per_deal: Optional[int] = Field(None, ge=0, le=50)
    escalation_threshold_days: Optional[int] = Field(None, ge=1, le=365)

    @field_validator("stale_thresholds")
    @classmethod
    def _validate_thresholds(cls, v):
        if v is None:
            return v
        for stage, days in v.items():
            if stage not in STAGE_ORDER:
                raise ValueError(
                    f"unknown stage '{stage}'; must be one of {STAGE_ORDER}"
                )
            if not isinstance(days, int) or isinstance(days, bool) or days < 0 or days > 365:
                raise ValueError(
                    f"threshold for '{stage}' must be an int between 0 and 365"
                )
        return v


class DealActionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    deal_id: uuid.UUID
    action_type: str
    status: str
    ai_reasoning: Optional[str] = None
    email_draft_id: Optional[uuid.UUID] = None
    suggested_action: Optional[str] = None
    executed_at: Optional[datetime] = None
    dismissed_at: Optional[datetime] = None
    created_at: datetime
    # Injected for the UI so the queue can name the deal without a second call.
    deal_title: Optional[str] = None
    company_name: Optional[str] = None


class AgentStats(BaseModel):
    deals_nudged: int
    followups_sent: int
    stage_advances: int
    at_risk_count: int
    pending_actions: int
    suggestions: int
    escalations: int
