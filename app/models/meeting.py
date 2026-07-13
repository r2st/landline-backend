"""Meeting brief model — pre-meeting intelligence & post-meeting analysis (2.5).

A ``MeetingBrief`` is a one-page, AI-assembled prep sheet for a booked (or
proposed) meeting: background, pain points, objection prep, competitive
positioning, a recommended agenda and talking points. After the meeting is
logged, ``post_meeting_analysis`` holds the AI's summary + follow-up actions.

Contacts are stored as a JSONB list of contact-id strings (a meeting can involve
several stakeholders), rather than a single FK.
"""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

MEETING_BRIEF_STATUSES = ("draft", "ready", "completed")


class MeetingBrief(Base):
    __tablename__ = "meeting_briefs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # List of contact-id strings involved in the meeting.
    contact_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="Meeting briefing")
    meeting_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    # Sections: background, pain_points, objection_prep, competitive_positioning,
    # recommended_agenda, talking_points.
    briefing_content: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ready")
    # Google Calendar event id when the meeting was scheduled through Landline.
    calendar_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Populated after the meeting: {summary, sentiment, action_items, next_steps}.
    post_meeting_analysis: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    model_used: Mapped[str | None] = mapped_column(String(50), nullable=True)
    generated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<MeetingBrief company={self.company_id} status={self.status}>"
