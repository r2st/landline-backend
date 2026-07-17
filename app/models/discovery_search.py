"""Saved Google Places discovery searches — the unit of scheduled SMB sourcing.

Places has no "give me every manufacturer in India" query. Coverage comes from
*many narrow* searches (a category x location grid), each capped at 60 results,
each billed. A saved search is one cell of that grid, so the schedule and the
credit spend are both attributable to a row an operator can see and switch off.

``schedule`` is the whole reason this table exists rather than a request body:
local business data changes slowly, so a weekly cadence finds nearly everything
a daily one would at a seventh of the cost.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import OrgScopedMixin

# How often the Celery beat sweep re-runs a search. ``one_time`` runs once and
# then stops re-running itself, but stays queryable as provenance.
DISCOVERY_SCHEDULES = ("one_time", "daily", "weekly")


class DiscoverySearch(Base, OrgScopedMixin):
    """A saved category x location Places search."""

    __tablename__ = "discovery_searches"
    __table_args__ = (
        CheckConstraint(
            "schedule IN ('one_time','daily','weekly')",
            name="ck_discovery_searches_schedule",
        ),
        # The beat sweep's selection is always (is_active, schedule) then
        # oldest-run-first.
        Index("ix_discovery_searches_active_schedule", "is_active", "schedule"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # The Places textQuery, e.g. "injection molding manufacturers".
    query: Mapped[str] = mapped_column(String(500), nullable=False)
    # Free text, e.g. "Pune, India". Folded into the textQuery — Places resolves
    # place names better than we could geocode them ourselves.
    location_text: Mapped[str | None] = mapped_column(String(255))
    # Only meaningful with lat/lng: Places biases a circle, and a circle needs a
    # centre. Radius without a centre is silently ignored by the API, so the
    # service treats it as such rather than pretending it filtered.
    radius_km: Mapped[int | None] = mapped_column(Integer)
    lat: Mapped[float | None] = mapped_column()
    lng: Mapped[float | None] = mapped_column()
    # A Places includedType, e.g. 'hardware_store'. Absent = the query alone.
    business_type: Mapped[str | None] = mapped_column(String(100))
    # ISO-3166-1 alpha-2. Stamped onto every business this search finds, because
    # Places' addressComponents don't always carry a country. Validated against
    # settings.supported_countries at the API boundary, and again by the
    # ingestion service on import — which rejects, never relabels.
    country: Mapped[str | None] = mapped_column(String(2))
    schedule: Mapped[str] = mapped_column(
        String(10), nullable=False, default="one_time", server_default="one_time"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    last_run_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), index=True
    )
    # Cumulative businesses this search has surfaced, across every run.
    results_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Outcome of the most recent run: ok | rate_limited | budget_exhausted |
    # error | skipped. A rate-limited search must never read as "found nothing".
    last_status: Mapped[str | None] = mapped_column(String(20))
    last_error: Mapped[str | None] = mapped_column(Text)
    credits_spent: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:
        return f"<DiscoverySearch {self.name!r} {self.schedule} active={self.is_active}>"
