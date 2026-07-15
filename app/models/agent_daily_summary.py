"""Agent daily-activity summary — a persisted daily digest of every autonomous
action the system took (leads discovered/enriched, emails sent by sequence and
stage, bounces, replies, follow-ups, qualification changes, calls queued, and
overall deliverability). One row per calendar day.

The heavy metrics live in the ``metrics`` JSONB blob so the shape can evolve
without a migration; ``narrative`` is the human-readable "here's what the agent
did today" text (rule-based, optionally AI-enhanced). A handful of headline
numbers are also promoted to real columns so the trend charts and history list
can be queried/sorted without unpacking JSON.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import Date, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentDailySummary(Base):
    __tablename__ = "agent_daily_summaries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # The calendar day this summary covers (in the reporting timezone). Unique so
    # a re-run for the same day upserts rather than duplicating.
    summary_date: Mapped[date] = mapped_column(
        Date, nullable=False, unique=True, index=True
    )

    # Full metrics payload — see app/services/daily_summary.py for the shape.
    metrics: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Human-readable narrative of what the agent did.
    narrative: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # --- Promoted headline metrics (for cheap trend queries / sorting) ---
    emails_sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    leads_discovered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    replies_received: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bounces_received: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    follow_ups_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    calls_queued: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<AgentDailySummary {self.summary_date} sent={self.emails_sent}>"
