"""Lead scoring model — stores rule-based and AI scores for companies."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class LeadScore(Base):
    __tablename__ = "lead_scores"
    __table_args__ = (
        CheckConstraint("score_type IN ('rule_based', 'ai')", name="ck_lead_scores_type"),
        CheckConstraint("fit_score BETWEEN 0 AND 100", name="ck_lead_scores_fit"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    score_type: Mapped[str] = mapped_column(String(20), nullable=False)
    fit_score: Mapped[int] = mapped_column(Integer, nullable=False)
    industry_score: Mapped[int] = mapped_column(Integer, default=0)
    size_score: Mapped[int] = mapped_column(Integer, default=0)
    location_score: Mapped[int] = mapped_column(Integer, default=0)
    engagement_score: Mapped[int] = mapped_column(Integer, default=0)
    factors: Mapped[dict] = mapped_column(JSONB, default=dict)
    scored_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    # Relationships
    company: Mapped["Company"] = relationship("Company", back_populates="lead_scores")

    def __repr__(self) -> str:
        return f"<LeadScore company={self.company_id} fit={self.fit_score}>"
