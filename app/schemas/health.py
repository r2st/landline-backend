"""Pydantic schemas for client health scoring."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class HealthComponents(BaseModel):
    """Per-factor contribution to the overall health score (each capped)."""

    recency: float       # how recently the client was contacted (max 40)
    responsiveness: float  # email reply rate (max 30)
    engagement: float    # meeting frequency (max 30)


class HealthSignals(BaseModel):
    """The raw activity signals the score was computed from."""

    last_contact_at: Optional[datetime] = None
    days_since_last_contact: Optional[int] = None
    emails_sent: int
    replies: int
    response_rate: float        # 0..1
    meetings_last_90d: int


class ClientHealth(BaseModel):
    company_id: uuid.UUID
    company_name: Optional[str] = None
    health_status: str          # healthy / at_risk / churned
    score: int                  # 0..100
    components: HealthComponents
    signals: HealthSignals
    computed_at: datetime
