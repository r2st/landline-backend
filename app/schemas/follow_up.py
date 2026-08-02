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

    # Set when this task was filed by a non-email sequence step. The enrollment
    # is holding on it: marking it done advances the cadence and spaces the next
    # email from the completion. Surfaced so a task list can say that out loud —
    # "this is step 3 of Injection Molding Q3" is a different kind of to-do from
    # "call Bob back", and the UI has no other way to tell them apart.
    enrollment_id: Optional[uuid.UUID] = None
    step_number: Optional[int] = None
    channel: Optional[str] = None


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
