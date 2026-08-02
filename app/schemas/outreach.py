"""Pydantic schemas for outreach sequences and enrollments."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.sequence import (
    DEFAULT_SEQUENCE_TONE,
    SEND_CONDITIONS,
    SEQUENCE_TONES,
    STEP_CHANNELS,
)
from app.services.step_delay import MAX_JITTER_MINUTES

# Literal-ish validation without importing Literal for every tuple: the model
# module owns the vocabulary, the schema reuses it, so they can't drift.
_TONE_PATTERN = f"^({'|'.join(SEQUENCE_TONES)})$"
_TONE_DESC = f"Voice the AI copywriter writes this sequence in. One of: {', '.join(SEQUENCE_TONES)}."
_CHANNEL_PATTERN = f"^({'|'.join(STEP_CHANNELS)})$"
_CONDITION_PATTERN = f"^({'|'.join(SEND_CONDITIONS)})$"


# --- Sequence Steps ---

class SequenceStepCreate(BaseModel):
    step_number: int = Field(..., ge=1)
    delay_days: int = Field(0, ge=0, description="Days to wait after the previous touch.")
    delay_hours: int = Field(
        0, ge=0, le=23,
        description="Hours to wait on top of delay_days. Capped at 23 — a full "
                    "day belongs in delay_days, where business-day counting can "
                    "see it.",
    )
    delay_business_days: bool = Field(
        False,
        description="Count delay_days in business days, skipping Sat/Sun. The "
                    "send window already blocks weekend sends, so without this "
                    "a Thursday +3 and a Friday +2 both land on the same Monday.",
    )
    delay_jitter_minutes: int = Field(
        0, ge=0, le=MAX_JITTER_MINUTES,
        description="Spread the due time across this many minutes, derived from "
                    "the enrollment so it is stable. Stops a batch enrolled "
                    "together from coming due in one identical block.",
    )
    subject_template: str
    body_template: str
    # The step model has carried these three since the multi-channel and
    # conditional-branching work landed, and the send path honours all of them —
    # but no request schema accepted them, so the only cadence this API could
    # build was unconditional single-channel email. The capability was there and
    # unreachable, which is indistinguishable from absent to anyone using it.
    channel: str = Field(
        "email", pattern=_CHANNEL_PATTERN,
        description="How this touch is made. One of: " + ", ".join(STEP_CHANNELS) +
                    ". Anything other than 'email' is a task for a human — the "
                    "cadence files a FollowUp and holds until it is done or "
                    "expires (see app.services.manual_touch).",
    )
    send_condition: str = Field(
        "always", pattern=_CONDITION_PATTERN,
        description="Gate evaluated against the prior step's engagement before "
                    "this step sends. One of: " + ", ".join(SEND_CONDITIONS) + ".",
    )
    task_timeout_hours: Optional[int] = Field(
        None, ge=1,
        description="How long a non-email step waits for the human before the "
                    "touch is recorded as not made and the cadence moves on. "
                    "NULL uses the org default. Ignored for email steps.",
    )


class SequenceStepRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    step_number: int
    delay_days: int
    delay_hours: int = 0
    delay_business_days: bool = False
    delay_jitter_minutes: int = 0
    subject_template: str
    body_template: str
    channel: str = "email"
    send_condition: str = "always"
    task_timeout_hours: Optional[int] = None


# --- Sequences ---

class SequenceCreate(BaseModel):
    name: str = Field(..., max_length=255)
    description: Optional[str] = None
    tone: str = Field(DEFAULT_SEQUENCE_TONE, pattern=_TONE_PATTERN, description=_TONE_DESC)
    steps: list[SequenceStepCreate] = Field(default_factory=list)


class SequenceUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = None
    is_active: Optional[bool] = None
    # Optional-with-no-default so ``exclude_unset`` can tell "set it to friendly"
    # apart from "don't touch the tone" — the update endpoint patches only the
    # fields the caller actually sent.
    tone: Optional[str] = Field(None, pattern=_TONE_PATTERN, description=_TONE_DESC)


class SequenceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: Optional[str] = None
    is_active: bool
    tone: str = DEFAULT_SEQUENCE_TONE
    created_at: datetime
    steps: list[SequenceStepRead] = []
    # Derived from the steps, never stored — see the note where
    # ``cadence_pattern`` used to live in app/models/sequence.py.
    channels: list[str] = []


class SequenceWithCounts(BaseModel):
    """A sequence summarized for the picker — includes step and enrollment counts."""

    id: uuid.UUID
    name: str
    description: Optional[str] = None
    industry: Optional[str] = None
    is_active: bool
    status: str = "active"
    tone: str = DEFAULT_SEQUENCE_TONE
    created_at: datetime
    step_count: int
    enrollment_count: int
    active_enrollment_count: int


class SequenceStatusUpdate(BaseModel):
    status: str = Field(..., pattern="^(active|paused)$")


# --- Industry verticals ---

class IndustryVerticalRead(BaseModel):
    """An industry vertical for the picker: copy metadata + the seeded sequence."""

    key: str
    name: str
    description: str
    demo_link: str
    pain_points: list[str]
    keywords: list[str]
    step_count: int
    sequence_id: Optional[uuid.UUID] = None  # None until seeded
    sequence_name: str


# Largest batch one enroll call may carry.
#
# Enrolling is not a set-based UPDATE: every id costs an insert and a Celery
# dispatch, plus a research-brief dispatch per distinct company, all inline in
# the request. An uncapped list is therefore an unbounded amount of work an
# anonymous body can ask for — the same shape as the CSV import that could hand
# a worker two million rows. It also runs into a hard wall well before that: the
# `IN (...)` lookups bind one parameter per id, and PostgreSQL's wire protocol
# tops out at 32767, so a large-enough list fails with a driver error rather
# than a usable message.
#
# 1000 matches the cap already on the contact bulk-tag and bulk-suppress bodies;
# a batch bigger than that is a campaign import, not a UI selection.
MAX_ENROLL_BATCH = 1000


class EnrollByIndustryRequest(BaseModel):
    industry: str = Field(..., description="Industry vertical key, e.g. 'cnc_machining'")
    contact_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=MAX_ENROLL_BATCH)


class SuggestIndustryResult(BaseModel):
    """Suggested vertical for a company/contact, with the resolved sequence."""

    industry: Optional[str] = None       # None when nothing matched a vertical
    name: Optional[str] = None
    matched: bool
    sequence_id: Optional[uuid.UUID] = None
    sequence_name: Optional[str] = None
    demo_link: Optional[str] = None


# --- Batch enrollment ---

class EnrollBatchRequest(BaseModel):
    contact_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=MAX_ENROLL_BATCH)


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
