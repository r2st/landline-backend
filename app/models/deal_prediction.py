"""Deal prediction model — AI/ML-driven close-probability and risk forecasting.

One row per scoring run for a deal (append-only history, newest wins). Stores the
statistical close probability (from the gradient-boosted model or the heuristic
fallback), the AI-adjusted composite confidence, the predicted close date, and
the risk assessment (stalling / at-risk flags with contributing factors).

Powers ``GET /deals/{id}/prediction`` and the aggregate ``GET /deals/forecast``.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Float, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class DealPrediction(Base):
    __tablename__ = "deal_predictions"
    __table_args__ = (
        CheckConstraint(
            "risk_level IN ('on_track','watch','at_risk','stalling')",
            name="ck_deal_predictions_risk_level",
        ),
        CheckConstraint(
            "close_probability BETWEEN 0 AND 100",
            name="ck_deal_predictions_probability",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    deal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("deals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Composite close probability (0-100) — the AI-adjusted number the UI shows.
    close_probability: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # The raw statistical probability before Claude's qualitative adjustment.
    model_probability: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # Claude's qualitative confidence (0-100), null when AI was not consulted.
    ai_confidence: Mapped[float | None] = mapped_column(Float)
    predicted_close_date: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    # Risk assessment.
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False, default="on_track")
    risk_flags: Mapped[list] = mapped_column(JSONB, default=list)
    # e.g. ["no_meeting_14d", "sentiment_declining", "stage_stalled_30d"]

    # Feature contributions / reasoning for explainability.
    contributing_factors: Mapped[dict] = mapped_column(JSONB, default=dict)
    ai_summary: Mapped[str | None] = mapped_column(Text)

    # Which model produced this — "gbt_v1" (trained), "heuristic_v1" (fallback).
    model_version: Mapped[str] = mapped_column(String(30), nullable=False, default="heuristic_v1")

    predicted_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    # Relationships
    deal: Mapped["Deal"] = relationship("Deal")

    def __repr__(self) -> str:
        return f"<DealPrediction deal={self.deal_id} p={self.close_probability} {self.risk_level}>"
