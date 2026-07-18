"""Pydantic schemas for the Referral & Warm-Intro agent API."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.referral import (
    REFERRAL_ACTION_TYPES,
    REFERRAL_LLM_MODELS,
    REFERRAL_STATUSES,
)

# Re-exported so the router and tests share one source of truth.
LLM_MODELS = list(REFERRAL_LLM_MODELS)
STATUSES = list(REFERRAL_STATUSES)
ACTION_TYPES = list(REFERRAL_ACTION_TYPES)


# --- Agent config ---------------------------------------------------------
class AgentConfigRead(BaseModel):
    """Agent config as returned to the client. ``llm_api_key`` is never sent
    back — only ``has_api_key`` indicates whether one is stored."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    is_active: bool
    llm_model: str
    has_api_key: bool = False
    min_health_score: int
    min_relationship_days: int
    max_asks_per_client: int
    followup_after_days: int
    auto_ask_enabled: bool
    referral_template: Optional[str] = None
    followup_template: Optional[str] = None
    thankyou_template: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    available_models: list[str] = Field(default_factory=lambda: list(LLM_MODELS))


class AgentConfigUpdate(BaseModel):
    """Partial update. ``llm_api_key`` set to "" clears the stored key; omitted
    (None) leaves it unchanged; a non-empty value replaces it (stored encrypted)."""

    is_active: Optional[bool] = None
    llm_model: Optional[str] = None
    llm_api_key: Optional[str] = None
    min_health_score: Optional[int] = Field(None, ge=0, le=100)
    min_relationship_days: Optional[int] = Field(None, ge=0, le=3650)
    max_asks_per_client: Optional[int] = Field(None, ge=1, le=20)
    followup_after_days: Optional[int] = Field(None, ge=1, le=180)
    auto_ask_enabled: Optional[bool] = None
    referral_template: Optional[str] = None
    followup_template: Optional[str] = None
    thankyou_template: Optional[str] = None


# --- Referrals ------------------------------------------------------------
class ReferralRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    referrer_contact_id: Optional[uuid.UUID] = None
    referrer_name: Optional[str] = None
    referred_name: Optional[str] = None
    referred_email: Optional[str] = None
    referred_company: Optional[str] = None
    referred_contact_id: Optional[uuid.UUID] = None
    status: str
    ask_sent_at: Optional[datetime] = None
    received_at: Optional[datetime] = None
    converted_at: Optional[datetime] = None
    referral_value: Optional[int] = None
    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class ReferralCreate(BaseModel):
    referrer_contact_id: Optional[uuid.UUID] = None
    referred_name: str = Field(..., min_length=1, max_length=200)
    referred_email: Optional[str] = Field(None, max_length=255)
    referred_company: Optional[str] = Field(None, max_length=255)
    status: str = "received"
    referral_value: Optional[int] = Field(None, ge=0)
    notes: Optional[str] = None


class ReferralUpdate(BaseModel):
    status: Optional[str] = None
    referred_name: Optional[str] = None
    referred_email: Optional[str] = None
    referred_company: Optional[str] = None
    referral_value: Optional[int] = Field(None, ge=0)
    notes: Optional[str] = None


# --- Actions --------------------------------------------------------------
class ActionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    referrer_contact_id: Optional[uuid.UUID] = None
    referrer_name: Optional[str] = None
    referral_id: Optional[uuid.UUID] = None
    action_type: str
    status: str
    ai_subject: Optional[str] = None
    ai_message: Optional[str] = None
    executed_at: Optional[datetime] = None
    created_at: datetime


class ActionResult(BaseModel):
    action: ActionRead
    send_status: Optional[str] = None
    detail: Optional[str] = None


# --- Stats ----------------------------------------------------------------
class ReferralStats(BaseModel):
    identified: int = 0
    asked: int = 0
    pending: int = 0
    received: int = 0
    converted: int = 0
    declined: int = 0
    total: int = 0
    response_rate: float = 0.0        # (received + converted) / asked
    conversion_rate: float = 0.0      # converted / (received + converted)
    total_referral_value: int = 0     # paise
    pending_actions: int = 0
