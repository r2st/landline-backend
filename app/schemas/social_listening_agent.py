"""Pydantic schemas for the Social Listening Agent API (`/social-listening-agent`).

The agent config is one row per org (resolved from the caller's JWT), so no
``org_id`` appears in any path or body. The ``llm_api_key`` is write-only: it is
accepted on update, stored encrypted, and NEVER returned — reads expose only a
boolean ``has_api_key`` so the UI can show "using your key" vs "using the org
default".
"""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.social_listening_agent import (
    ALLOWED_LLM_MODELS,
    SIGNAL_STATUSES,
    SIGNAL_TYPES,
    SOURCE_TYPES,
)

# --- Agent config ------------------------------------------------------------


class AgentConfigRead(BaseModel):
    """The org's agent config as shown on the dashboard (key masked)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    is_active: bool
    llm_model: str
    has_api_key: bool
    monitored_keywords: list[str] = Field(default_factory=list)
    monitored_industries: list[str] = Field(default_factory=list)
    news_sources: list[str] = Field(default_factory=list)
    min_relevance_score: int
    auto_create_leads: bool
    auto_enroll_dialtone: bool
    scan_frequency_hours: int
    last_scan_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    # The model roster the dropdown should offer.
    available_models: list[str] = Field(default_factory=lambda: list(ALLOWED_LLM_MODELS))


class AgentConfigUpdate(BaseModel):
    """Fields an org_admin may change. All optional — partial updates supported.

    ``llm_api_key`` is write-only: ``None`` leaves the stored key untouched, an
    empty string clears it (fall back to the org/process key), any other value
    replaces it (stored encrypted).
    """

    is_active: Optional[bool] = None
    llm_model: Optional[str] = None
    llm_api_key: Optional[str] = None
    monitored_keywords: Optional[list[str]] = None
    monitored_industries: Optional[list[str]] = None
    news_sources: Optional[list[str]] = None
    min_relevance_score: Optional[int] = Field(default=None, ge=0, le=100)
    auto_create_leads: Optional[bool] = None
    auto_enroll_dialtone: Optional[bool] = None
    scan_frequency_hours: Optional[int] = Field(default=None, ge=1, le=168)


# --- Signals -----------------------------------------------------------------


class SignalRead(BaseModel):
    """A discovered signal card."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_type: str
    source_url: Optional[str] = None
    company_name: str
    company_domain: Optional[str] = None
    company_id: Optional[uuid.UUID] = None
    headline: str
    summary: Optional[str] = None
    relevance_score: int
    signal_type: Optional[str] = None
    status: str
    lead_created: bool
    contact_id: Optional[uuid.UUID] = None
    created_at: datetime


class SignalActionResponse(BaseModel):
    """Result of converting a signal to a lead."""

    signal: SignalRead
    lead_created: bool
    enrolled: bool
    contact_id: Optional[uuid.UUID] = None
    detail: str


# --- Monitored companies -----------------------------------------------------


class MonitoredCompanyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_name: str
    domain: Optional[str] = None
    company_id: Optional[uuid.UUID] = None
    monitor_news: bool
    monitor_jobs: bool
    last_checked_at: Optional[datetime] = None
    created_at: datetime


class MonitoredCompanyCreate(BaseModel):
    company_name: str = Field(min_length=1, max_length=255)
    domain: Optional[str] = Field(default=None, max_length=255)
    company_id: Optional[uuid.UUID] = None
    monitor_news: bool = True
    monitor_jobs: bool = True


# --- Stats -------------------------------------------------------------------


class SignalTypeCount(BaseModel):
    signal_type: str
    count: int


class TopCompanyCount(BaseModel):
    company_name: str
    count: int


class StatsRead(BaseModel):
    total_signals: int
    signals_this_week: int
    leads_created: int
    dismissed: int
    conversion_rate: float
    by_signal_type: list[SignalTypeCount] = Field(default_factory=list)
    top_companies: list[TopCompanyCount] = Field(default_factory=list)


# Exposed for router validation / tests.
__all__ = [
    "AgentConfigRead",
    "AgentConfigUpdate",
    "SignalRead",
    "SignalActionResponse",
    "MonitoredCompanyRead",
    "MonitoredCompanyCreate",
    "StatsRead",
    "SIGNAL_STATUSES",
    "SIGNAL_TYPES",
    "SOURCE_TYPES",
]
