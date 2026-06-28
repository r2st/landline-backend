"""Outreach sequence and step models."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class OutreachSequence(Base):
    __tablename__ = "outreach_sequences"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','paused','completed')",
            name="ck_sequence_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # Industry vertical key (see app.services.industry_verticals), e.g.
    # "injection_molding". NULL for general/custom sequences.
    industry: Mapped[str | None] = mapped_column(String(80))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    # Relationships
    steps: Mapped[list["SequenceStep"]] = relationship(
        "SequenceStep", back_populates="sequence", cascade="all, delete-orphan",
        order_by="SequenceStep.step_number", lazy="selectin"
    )
    enrollments: Mapped[list["OutreachEnrollment"]] = relationship(
        "OutreachEnrollment", back_populates="sequence", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<OutreachSequence {self.name}>"


class SequenceStep(Base):
    __tablename__ = "sequence_steps"
    __table_args__ = (
        UniqueConstraint("sequence_id", "step_number", name="uq_sequence_step"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    sequence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("outreach_sequences.id", ondelete="CASCADE"), nullable=False
    )
    step_number: Mapped[int] = mapped_column(Integer, nullable=False)
    delay_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    subject_template: Mapped[str] = mapped_column(Text, nullable=False)
    body_template: Mapped[str] = mapped_column(Text, nullable=False)

    # Relationships
    sequence: Mapped["OutreachSequence"] = relationship("OutreachSequence", back_populates="steps")

    def __repr__(self) -> str:
        return f"<SequenceStep seq={self.sequence_id} step={self.step_number}>"
