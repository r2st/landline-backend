"""Pydantic schemas for the Client Success Agent surface.

The config schemas deliberately never expose ``llm_api_key`` in full: reads
return only a boolean ``llm_api_key_set`` plus a masked hint, so a saved key is
visible-as-present but never re-transmitted. Writes accept the raw key (which
the router encrypts before persisting) and treat an empty string as "clear it,
fall back to the org key".
"""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

# The OpenRouter models offered in the config dropdown. Free-tier ids so an org
# can run the agent at zero cost; a custom id can still be sent and is accepted.
LLM_MODEL_OPTIONS = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "openai/gpt-oss-20b:free",
    "openai/gpt-oss-120b:free",
    "google/gemma-3-27b-it:free",
]


class AgentConfigRead(BaseModel):
    """Agent config as returned to the client. Never carries the raw API key."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    is_active: bool
    llm_model: str
    # Whether a per-agent key is stored, plus a masked hint — but NEVER the key.
    llm_api_key_set: bool
    llm_api_key_hint: Optional[str] = None
    health_warning_threshold: int
    health_critical_threshold: int
    check_frequency_days: int
    auto_checkin_enabled: bool
    auto_escalation_enabled: bool
    celebration_enabled: bool
    checkin_template: Optional[str] = None
    escalation_template: Optional[str] = None
    celebration_template: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    # Convenience for the UI dropdown; static but handy alongside the config.
    model_options: list[str] = Field(default_factory=lambda: list(LLM_MODEL_OPTIONS))


class AgentConfigUpdate(BaseModel):
    """Partial update of the agent config (org_admin only).

    Every field is optional so the UI can PATCH-style send only what changed.
    ``llm_api_key`` is special: ``None`` leaves the stored key untouched, an
    empty string clears it (fall back to the org default), any other value
    replaces it (the router encrypts before persisting).
    """

    is_active: Optional[bool] = None
    llm_model: Optional[str] = Field(None, min_length=1, max_length=120)
    llm_api_key: Optional[str] = None
    health_warning_threshold: Optional[int] = Field(None, ge=0, le=100)
    health_critical_threshold: Optional[int] = Field(None, ge=0, le=100)
    check_frequency_days: Optional[int] = Field(None, ge=1, le=90)
    auto_checkin_enabled: Optional[bool] = None
    auto_escalation_enabled: Optional[bool] = None
    celebration_enabled: Optional[bool] = None
    checkin_template: Optional[str] = None
    escalation_template: Optional[str] = None
    celebration_template: Optional[str] = None


class ActionRead(BaseModel):
    """One Client Success action row, enriched with client display fields."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    contact_id: Optional[uuid.UUID] = None
    company_id: Optional[uuid.UUID] = None
    action_type: str
    trigger_reason: Optional[str] = None
    health_score_at_trigger: Optional[int] = None
    status: str
    ai_message: Optional[str] = None
    executed_at: Optional[datetime] = None
    dismissed_at: Optional[datetime] = None
    created_at: datetime
    # Denormalized display context (filled by the router, not columns).
    company_name: Optional[str] = None
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None


class ActionList(BaseModel):
    actions: list[ActionRead]
    total: int


class AgentStats(BaseModel):
    """Roll-up shown in the dashboard stat cards."""

    clients_monitored: int
    checkins_sent: int
    escalations: int
    celebrations: int
    pending_actions: int
    review_calls: int


class ClientHealthRow(BaseModel):
    """A client in the health overview, with its trend vs the prior snapshot."""

    company_id: uuid.UUID
    company_name: Optional[str] = None
    score: int
    health_status: str
    previous_score: Optional[int] = None
    trend: str  # 'up' | 'down' | 'flat'
    last_contact_date: Optional[datetime] = None


class ClientHealthOverview(BaseModel):
    clients: list[ClientHealthRow]
