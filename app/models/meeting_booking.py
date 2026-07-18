"""Meeting Booking Agent models.

Two tables back the autonomous scheduling agent that takes over once Dialtone
gets a positive reply to a cold email:

* :class:`MeetingBookingAgent` — one configuration row per organization: which
  connected calendar to book on, the meeting length, the buffer between
  meetings, the working-hours window and timezone the agent offers slots in, how
  far ahead it looks, a daily cap, and the confirmation/reminder copy.
* :class:`BookedMeeting` — one row per meeting the agent is working: the slots it
  offered, the running scheduling conversation, the resolved time, and the
  Google Calendar event once confirmed. Its ``status`` walks
  ``proposed → confirmed → completed`` (or ``cancelled`` / ``no_show``).

Both mix in :class:`OrgScopedMixin`, so every read/write is automatically scoped
to the caller's organization (and stamped on insert) exactly like the rest of
the tenant-owned tables.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import OrgScopedMixin

# Lifecycle of a single meeting the agent is handling. ``proposed`` = slots
# offered, awaiting the prospect's pick; ``confirmed`` = time agreed + calendar
# invite sent; ``completed`` = the meeting time has passed; ``cancelled`` /
# ``no_show`` are terminal manual/observed outcomes.
BOOKED_MEETING_STATUSES = (
    "proposed",
    "confirmed",
    "cancelled",
    "completed",
    "no_show",
)


class MeetingBookingAgent(Base, OrgScopedMixin):
    """Per-organization configuration for the autonomous meeting-booking agent.

    Exactly one row per org (enforced by the unique constraint on ``org_id``);
    the router lazily creates it with defaults the first time the org opens the
    dashboard. ``is_active`` is the master on/off switch — when False the agent
    never proposes a meeting even on a positive reply.
    """

    __tablename__ = "meeting_booking_agents"
    __table_args__ = (
        UniqueConstraint("org_id", name="uq_meeting_booking_agent_org"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # The connected Gmail account whose calendar the agent reads for free/busy
    # and books events on, and which sends the scheduling emails. NULL until the
    # operator picks one — the agent stays dormant without a calendar.
    calendar_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gmail_accounts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    default_meeting_duration_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default="30"
    )
    buffer_between_meetings_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=15, server_default="15"
    )
    # Working-hours window (local to ``timezone``), stored as "HH:MM" strings so
    # the operator edits them as wall-clock times without a tz conversion.
    working_hours_start: Mapped[str] = mapped_column(
        String(5), nullable=False, default="09:00", server_default="09:00"
    )
    working_hours_end: Mapped[str] = mapped_column(
        String(5), nullable=False, default="17:00", server_default="17:00"
    )
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="Asia/Kolkata", server_default="Asia/Kolkata"
    )
    # How many days ahead to offer slots.
    booking_window_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=7, server_default="7"
    )
    # Cap on confirmed meetings per local day — keeps the agent from stacking a
    # calendar and protects the rep's focus time.
    max_daily_meetings: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, server_default="5"
    )

    # Optional operator-authored copy. When set, used verbatim (with placeholder
    # substitution) instead of the built-in default text.
    confirmation_template: Mapped[str | None] = mapped_column(Text)
    reminder_template: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    calendar_account: Mapped["GmailAccount | None"] = relationship("GmailAccount")

    def __repr__(self) -> str:
        return f"<MeetingBookingAgent org={self.org_id} active={self.is_active}>"


class BookedMeeting(Base, OrgScopedMixin):
    """A single meeting the booking agent is proposing, has booked, or ran.

    ``proposed_slots`` records the slots offered (each ``{start, end, label}``)
    so a later reply can be matched against them; ``conversation_thread`` is an
    append-only list of ``{role, text, at}`` turns capturing the scheduling
    back-and-forth for the detail view and audit trail.
    """

    __tablename__ = "booked_meetings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    enrollment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("outreach_enrollments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # The account that sent the scheduling emails / owns the calendar event.
    sender_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gmail_accounts.id", ondelete="SET NULL"),
        nullable=True,
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False, default="Intro call")
    description: Mapped[str | None] = mapped_column(Text)

    start_time: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), index=True)
    end_time: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="Asia/Kolkata", server_default="Asia/Kolkata"
    )

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="proposed", server_default="proposed", index=True
    )
    # Google Calendar event id once the meeting is confirmed + created.
    calendar_event_id: Mapped[str | None] = mapped_column(String(255))

    # Slots we offered: a list of {"start", "end", "label"} (ISO strings).
    proposed_slots: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    # The scheduling conversation: list of {"role", "text", "at"} turns.
    conversation_thread: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")

    # Gmail threading so a follow-up reply can be tied back to this meeting and
    # the confirmation/reminder land in the same thread.
    gmail_thread_id: Mapped[str | None] = mapped_column(String(255), index=True)
    gmail_message_id: Mapped[str | None] = mapped_column(String(255))

    reminder_sent_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    contact: Mapped["Contact | None"] = relationship("Contact")

    def __repr__(self) -> str:
        return f"<BookedMeeting contact={self.contact_id} status={self.status} start={self.start_time}>"
