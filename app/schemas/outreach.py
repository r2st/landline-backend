"""Pydantic schemas for outreach sequences and enrollments."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# --- Sequence Steps ---

class SequenceStepCreate(BaseModel):
    step_number: int = Field(..., ge=1)
    delay_days: int = Field(0, ge=0)
    subject_template: str
    body_template: str


class SequenceStepRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    step_number: int
    delay_days: int
    subject_template: str
    body_template: str


# --- Sequences ---

class SequenceCreate(BaseModel):
    name: str = Field(..., max_length=255)
    description: Optional[str] = None
    steps: list[SequenceStepCreate] = Field(default_factory=list)


class SequenceUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = None
    is_active: Optional[bool] = None


class SequenceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: Optional[str] = None
    is_active: bool
    created_at: datetime
    steps: list[SequenceStepRead] = []


class SequenceWithCounts(BaseModel):
    """A sequence summarized for the picker — includes step and enrollment counts."""

    id: uuid.UUID
    name: str
    description: Optional[str] = None
    is_active: bool
    status: str = "active"
    created_at: datetime
    step_count: int
    enrollment_count: int
    active_enrollment_count: int


class SequenceStatusUpdate(BaseModel):
    status: str = Field(..., pattern="^(active|paused)$")


# --- Batch enrollment ---

class EnrollBatchRequest(BaseModel):
    contact_ids: list[uuid.UUID] = Field(..., min_length=1)


class EnrollSkip(BaseModel):
    contact_id: uuid.UUID
    reason: str


class EnrollBatchResult(BaseModel):
    sequence_id: uuid.UUID
    enrolled: list[uuid.UUID]
    skipped: list[EnrollSkip]
    enrolled_count: int
    skipped_count: int


# --- Enrollments ---

class EnrollmentCreate(BaseModel):
    contact_id: uuid.UUID
    sequence_id: uuid.UUID


class EnrollmentUpdate(BaseModel):
    status: str = Field(..., pattern="^(active|paused|completed|replied|bounced|unsubscribed)$")


class EnrollmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    contact_id: uuid.UUID
    sequence_id: uuid.UUID
    current_step: int
    status: str
    enrolled_at: datetime
    completed_at: Optional[datetime] = None
    gmail_thread_id: Optional[str] = None
    gmail_message_id: Optional[str] = None
    sender_account_id: Optional[uuid.UUID] = None
    next_send_at: Optional[datetime] = None


# --- Events ---

class OutreachEventCreate(BaseModel):
    enrollment_id: uuid.UUID
    step_number: int
    event_type: str = Field(..., pattern="^(sent|delivered|opened|clicked|replied|bounced|unsubscribed)$")
    event_metadata: dict = Field(default_factory=dict)


class OutreachEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    enrollment_id: uuid.UUID
    step_number: int
    event_type: str
    occurred_at: datetime
    event_metadata: dict
