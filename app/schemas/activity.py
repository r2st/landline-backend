"""Pydantic schemas for the unified company activity feed."""

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel

# The activity types the feed can emit / be filtered by.
ACTIVITY_TYPES = (
    "email_sent",
    "email_opened",
    "email_clicked",
    "reply",
    "outreach_event",
    "score_change",
    "news",
    "note",
    "contact_added",
)


class ActivityItem(BaseModel):
    """One entry in a company's chronological activity feed."""

    id: str
    type: str
    timestamp: datetime
    title: str
    description: Optional[str] = None
    # Type-specific extras (contact name, subject, score, url, …).
    metadata: dict[str, Any] = {}


class ActivityFeed(BaseModel):
    company_id: uuid.UUID
    items: list[ActivityItem]
    total: int
    page: int
    page_size: int
