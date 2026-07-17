"""Rep profile model — per-user sales expertise, win rates, and capacity.

Backs intelligent lead routing (Feature 1.3): the router matches a lead to the
best rep by industry expertise and historical win-rate-by-segment, weighted by
current workload. One row per user; ``win_rate_by_segment`` and the aggregate
counters are recomputed periodically from closed deals.
"""

import uuid
from datetime import datetime

from sqlalchemy import Float, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP, JSONB, ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import OrgScopedMixin


class RepProfile(Base, OrgScopedMixin):
    __tablename__ = "rep_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    # Industry segments this rep is strong in (free-form or NAICS-segment labels).
    industries: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    # Win/loss counts and rate per segment: {segment: {won, lost, rate}}.
    win_rate_by_segment: Mapped[dict] = mapped_column(JSONB, default=dict)
    total_won: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_lost: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Overall win rate (0-1) across all segments.
    overall_win_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # Average days to close a won deal.
    avg_deal_velocity_days: Mapped[float | None] = mapped_column(Float)
    # Workload management — soft cap on concurrently-owned active leads.
    max_capacity: Mapped[int] = mapped_column(Integer, nullable=False, default=25)
    # Current active load (recomputed on refresh; a cached convenience count).
    active_load: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    user: Mapped["User"] = relationship("User")

    @property
    def available_capacity(self) -> int:
        return max(0, self.max_capacity - self.active_load)

    def __repr__(self) -> str:
        return f"<RepProfile user={self.user_id} win_rate={self.overall_win_rate:.2f}>"
