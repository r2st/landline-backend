"""Pydantic schemas for the smart-suggestions (next-best-action) feed."""

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

SUGGESTION_TYPES = (
    "overdue_follow_up",
    "stale_lead",
    "recent_news",
    "high_score",
)

PRIORITIES = ("high", "medium", "low")


class Suggestion(BaseModel):
    """A single next-best-action recommendation."""

    id: str
    type: str
    priority: str
    title: str
    reason: str
    action: str
    company_id: Optional[uuid.UUID] = None
    company_name: Optional[str] = None
    contact_id: Optional[uuid.UUID] = None
    contact_name: Optional[str] = None
    # Timestamp of the signal that produced this suggestion (for sorting/display).
    signal_at: Optional[datetime] = None
    metadata: dict[str, Any] = {}


class SuggestionsResponse(BaseModel):
    suggestions: list[Suggestion]
    total: int
    counts_by_type: dict[str, int]


# --- AI-powered suggestions (Feature 1.6) ---------------------------------

class AISuggestion(BaseModel):
    """An AI-generated, cross-signal next-best-action."""

    id: str
    type: str
    priority: str
    title: str
    reason: str
    action: str
    timing: str  # today | this_week | next_week
    company_id: Optional[uuid.UUID] = None
    company_name: Optional[str] = None
    recommended_rep_id: Optional[uuid.UUID] = None
    confidence: float = 0.7
    metadata: dict[str, Any] = {}


class AISuggestionsResponse(BaseModel):
    suggestions: list[AISuggestion]
    total: int
    source: str  # "ai" | "rules"
    generated_at: datetime


class SuggestionOutcomeCreate(BaseModel):
    """Record that a suggestion was surfaced / acted on (feedback loop)."""

    suggestion_key: str
    suggestion_type: str
    status: str = "accepted"  # suggested | accepted | dismissed | completed
    action: Optional[str] = None
    outcome: Optional[str] = None
    company_id: Optional[uuid.UUID] = None
    contact_id: Optional[uuid.UUID] = None
    payload: dict[str, Any] = {}
    notes: Optional[str] = None


class SuggestionOutcomeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    suggestion_key: str
    suggestion_type: str
    status: str
    action: Optional[str] = None
    outcome: Optional[str] = None
    company_id: Optional[uuid.UUID] = None
    contact_id: Optional[uuid.UUID] = None
    payload: dict[str, Any] = {}
    notes: Optional[str] = None
    created_at: datetime
    acted_at: Optional[datetime] = None
    updated_at: datetime
