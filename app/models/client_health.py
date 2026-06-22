"""Client-health snapshot model.

A point-in-time record of a client's health, persisted so engagement can be
tracked *over time*. The score/status and the activity-derived metrics
(``last_contact_date``, ``response_rate``, ``meeting_frequency``) are computed
from activity data; ``satisfaction_score`` is an optional manual signal that
can't be derived from activity (e.g. a CSAT/NPS-style rating).
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Float, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ClientHealthSnapshot(Base):
    __tablename__ = "client_health"
    __table_args__ = (
        CheckConstraint(
            "health_status IN ('healthy','at_risk','churned')",
            name="ck_client_health_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    # Overall computed health (0-100) and bucketed status.
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    health_status: Mapped[str] = mapped_column(String(20), nullable=False)
    # Activity-derived engagement metrics captured at snapshot time.
    last_contact_date: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    response_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    meeting_frequency: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Optional manual signal (e.g. CSAT 0-100); not derivable from activity.
    satisfaction_score: Mapped[float | None] = mapped_column(Float)
    # When this snapshot was taken (the axis for over-time tracking).
    recorded_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    # Relationships
    company: Mapped["Company"] = relationship("Company")

    def __repr__(self) -> str:
        return f"<ClientHealthSnapshot {self.company_id} {self.health_status}/{self.score}>"
