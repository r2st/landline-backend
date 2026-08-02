"""Follow-up reminder model — schedule and track outreach follow-ups.

A follow-up is a lightweight reminder attached to a contact (and/or its
company) telling the user to do something by ``due_date`` — e.g. "send a
second touch", "call back after the demo". It powers the follow-up tracking
view: overdue reminders, upcoming reminders, and "contacts that have gone
cold" (surfaced separately via the stale-contacts query).
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import OrgScopedMixin

# Forward reference for type hints
# from app.models.user import User  # imported lazily via string ref


class FollowUp(Base, OrgScopedMixin):
    __tablename__ = "follow_ups"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','completed','dismissed')",
            name="ck_follow_up_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=True
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=True
    )
    # --- Link back to the sequence step that asked for this touch ---
    #
    # Set only for the reminders a non-email sequence step files ("call them",
    # "send the InMail"); NULL for every hand-made follow-up, which is most of
    # them. When set, this task is the cadence: the enrollment holds at
    # ``step_number`` until the task reaches a terminal state, so marking it done
    # spaces the next email from when the call actually happened rather than from
    # when the reminder was filed. See ``app.services.manual_touch``.
    enrollment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("outreach_enrollments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    step_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The step's channel — "call" / "linkedin" / "video" / "direct_mail". Stored
    # rather than read through the step because it is what the task list needs to
    # show, and it must survive the step being edited under a task already filed.
    channel: Mapped[str | None] = mapped_column(String(20), nullable=True)
    due_date: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    # Relationships
    user: Mapped["User | None"] = relationship("User")
    contact: Mapped["Contact | None"] = relationship("Contact")
    company: Mapped["Company | None"] = relationship("Company")

    def __repr__(self) -> str:
        return f"<FollowUp contact={self.contact_id} due={self.due_date} status={self.status}>"
