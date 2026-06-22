"""Pydantic schemas for the smart-suggestions (next-best-action) feed."""

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel

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
