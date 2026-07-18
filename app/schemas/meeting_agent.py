"""Pydantic schemas for the Meeting Booking Agent API (``/meeting-agent``)."""

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MeetingAgentConfig(BaseModel):
    """The org's booking-agent configuration (read model)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    is_active: bool
    calendar_account_id: Optional[UUID] = None
    default_meeting_duration_minutes: int
    buffer_between_meetings_minutes: int
    working_hours_start: str
    working_hours_end: str
    timezone: str
    booking_window_days: int
    max_daily_meetings: int
    confirmation_template: Optional[str] = None
    reminder_template: Optional[str] = None
    updated_at: Optional[datetime] = None


class MeetingAgentConfigUpdate(BaseModel):
    """Settable fields on the agent config (write model).

    Every field is optional so a caller can patch just the on/off toggle or just
    one setting; an unset field is left untouched. Bounds keep the scheduler
    sane (a 0-minute meeting or a 400-day window is a config error).
    """

    is_active: Optional[bool] = None
    calendar_account_id: Optional[UUID] = None
    default_meeting_duration_minutes: Optional[int] = Field(None, ge=5, le=480)
    buffer_between_meetings_minutes: Optional[int] = Field(None, ge=0, le=240)
    working_hours_start: Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")
    working_hours_end: Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")
    timezone: Optional[str] = Field(None, max_length=64)
    booking_window_days: Optional[int] = Field(None, ge=1, le=60)
    max_daily_meetings: Optional[int] = Field(None, ge=1, le=50)
    confirmation_template: Optional[str] = None
    reminder_template: Optional[str] = None


class ProposedSlot(BaseModel):
    start: str
    end: Optional[str] = None
    label: Optional[str] = None


class ConversationTurn(BaseModel):
    role: str
    text: str
    at: Optional[str] = None
    kind: Optional[str] = None


class BookedMeetingSummary(BaseModel):
    """One row in the meetings list."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    contact_id: Optional[UUID] = None
    enrollment_id: Optional[UUID] = None
    title: str
    status: str
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    timezone: str
    calendar_event_id: Optional[str] = None
    reminder_sent_at: Optional[datetime] = None
    created_at: datetime
    # Denormalized contact fields for the list (filled by the router).
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None


class BookedMeetingDetail(BookedMeetingSummary):
    """Full meeting detail incl. the offered slots and the scheduling thread."""

    description: Optional[str] = None
    proposed_slots: List[ProposedSlot] = []
    conversation_thread: List[ConversationTurn] = []
    gmail_thread_id: Optional[str] = None


class MeetingListResponse(BaseModel):
    items: List[BookedMeetingSummary]
    total: int


class MeetingAgentStats(BaseModel):
    """Booking funnel + outcome stats for the dashboard cards."""

    total_meetings: int
    proposed: int
    confirmed: int
    completed: int
    cancelled: int
    no_show: int
    # Booking rate = confirmed+completed over everything we proposed.
    booking_rate: float
    # Show rate = completed over (completed + no_show).
    show_rate: float
    # Average hours from proposal to confirmation.
    avg_hours_to_book: Optional[float] = None
    upcoming: int
