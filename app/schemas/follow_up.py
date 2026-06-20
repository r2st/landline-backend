"""Pydantic schemas for follow-up reminders."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

VALID_FOLLOW_UP_STATUS = ("pending", "completed", "dismissed")


class FollowUpCreate(BaseModel):
    contact_id: Optional[uuid.UUID] = None
    company_id: Optional[uuid.UUID] = None
    due_date: datetime
    reason: Optional[str] = None
    notes: Optional[str] = None


class FollowUpUpdate(BaseModel):
    due_date: Optional[datetime] = None
    reason: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[str] = Field(None, pattern="^(pending|completed|dismissed)$")


class FollowUpRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    contact_id: Optional[uuid.UUID] = None
    company_id: Optional[uuid.UUID] = None
    due_date: datetime
    reason: Optional[str] = None
    status: str
    notes: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None
    # Convenience fields populated by the router for display.
    contact_name: Optional[str] = None
    company_name: Optional[str] = None
    is_overdue: bool = False


class StaleContact(BaseModel):
    """A contact with an email that hasn't been emailed within the threshold."""

    contact_id: uuid.UUID
    company_id: uuid.UUID
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    title: Optional[str] = None
    company_name: Optional[str] = None
    last_emailed_at: Optional[datetime] = None
    days_since_contact: Optional[int] = None
    has_open_follow_up: bool = False
