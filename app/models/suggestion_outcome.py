"""Suggestion outcome model — feedback loop for the smart-suggestions engine.

Records each AI-generated next-best-action that was surfaced to a user, whether
they acted on it, and what resulted. This closes the loop: the suggestions
service reads historical outcomes to calibrate priority (suggestion types that
lead to replies/meetings get boosted; ones that get dismissed get suppressed).
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import OrgScopedMixin


class SuggestionOutcome(Base, OrgScopedMixin):
    __tablename__ = "suggestion_outcomes"
    __table_args__ = (
        CheckConstraint(
            "status IN ('suggested','accepted','dismissed','completed')",
            name="ck_suggestion_outcomes_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # The suggestion's stable id (e.g. "ai:call:<company>" or "high_score:<uuid>").
    suggestion_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    suggestion_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str | None] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="suggested", index=True)
    # Result once acted on: reply, meeting_booked, converted, no_response, etc.
    outcome: Mapped[str | None] = mapped_column(String(50))
    # Full snapshot of the suggestion (title, reason, priority, metadata).
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    acted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    user: Mapped["User | None"] = relationship("User")
    company: Mapped["Company | None"] = relationship("Company")

    def __repr__(self) -> str:
        return f"<SuggestionOutcome {self.suggestion_type} {self.status}/{self.outcome}>"
