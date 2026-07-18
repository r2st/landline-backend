"""Schemas for the sender warm-up API (Feature 1)."""

import uuid
from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field


class WarmupPlanRead(BaseModel):
    """A sender's warm-up plan, annotated with today's usage."""

    id: uuid.UUID
    account_id: uuid.UUID
    account_type: str
    account_email: Optional[str] = None
    start_date: date
    current_day: int
    daily_limit: int
    status: str
    # Today's sends and headroom for the account (from the account's own daily
    # counter), so the UI can draw a progress bar without a second call.
    used_today: int = 0
    remaining_today: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class WarmupPlanCreate(BaseModel):
    """Create a warm-up plan for a sending account."""

    account_id: uuid.UUID
    # gmail | smtp — inferred from which table the account lives in when omitted.
    account_type: Optional[str] = Field(default=None)
    # Ramp start; defaults to today (day 1) when omitted.
    start_date: Optional[date] = None
