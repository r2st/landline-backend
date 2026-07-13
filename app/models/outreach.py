"""Outreach enrollment and event models."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class OutreachEnrollment(Base):
    __tablename__ = "outreach_enrollments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','paused','completed','replied','bounced','unsubscribed')",
            name="ck_enrollment_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    sequence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("outreach_sequences.id", ondelete="CASCADE"), nullable=False
    )
    current_step: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="active")
    enrolled_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    # Threading: chain follow-up emails into the same Gmail thread
    gmail_thread_id: Mapped[str | None] = mapped_column(String(255))
    gmail_message_id: Mapped[str | None] = mapped_column(String(255))
    sender_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("gmail_accounts.id", ondelete="SET NULL")
    )
    next_send_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    # AI-Optimized Sequence Engine (feature 1.2): per-enrollment record of the
    # optimizer's decisions and reasoning. Each entry is one decision dict
    # ({step, action, reasoning, ...}) appended by sequence_optimizer before a
    # send, so the sequence's AI behaviour is auditable and A/B-comparable.
    optimization_context: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")

    # Relationships
    contact: Mapped["Contact"] = relationship("Contact", back_populates="enrollments")
    sequence: Mapped["OutreachSequence"] = relationship("OutreachSequence", back_populates="enrollments")
    events: Mapped[list["OutreachEvent"]] = relationship(
        "OutreachEvent", back_populates="enrollment", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<OutreachEnrollment contact={self.contact_id} status={self.status}>"


class OutreachEvent(Base):
    __tablename__ = "outreach_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('sent','delivered','opened','clicked','replied','bounced','unsubscribed')",
            name="ck_event_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    enrollment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("outreach_enrollments.id", ondelete="CASCADE"), nullable=False
    )
    step_number: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    event_metadata: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)

    # Relationships
    enrollment: Mapped["OutreachEnrollment"] = relationship("OutreachEnrollment", back_populates="events")

    def __repr__(self) -> str:
        return f"<OutreachEvent {self.event_type} step={self.step_number}>"
