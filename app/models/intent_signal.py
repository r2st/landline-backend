"""Intent signal model — external buyer-intent signals for a company.

An IntentSignal records a detected event that indicates a company may be
entering (or already in) a buying cycle: a relevant job posting, a funding
round, an expansion announcement, a leadership change, or a technology signal.

Signals carry two AI-assessed 0-100 scores:

* ``relevance_score`` — how relevant this signal is to Apprend's value prop.
* ``urgency_score``   — how time-sensitive the resulting buying window is.

The combined score feeds lead scoring (see ``services/scoring.py``). Signals
expire (``expires_at``) so a stale six-month-old job posting stops inflating a
lead's score.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# Canonical signal types. Kept as a plain tuple (not a DB enum) so new signal
# categories can be added without a migration.
SIGNAL_TYPES = (
    "hiring",
    "expansion",
    "funding",
    "technology",
    "leadership",
    "regulatory",
    "m&a",
    "general",
)


class IntentSignal(Base):
    __tablename__ = "intent_signals"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    signal_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    source: Mapped[str | None] = mapped_column(String(200), nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    raw_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    ai_analysis: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    relevance_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 0-100
    urgency_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 0-100
    # AI-inferred number of days until the buying window opens/closes (nullable).
    buying_window_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recommended_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    outreach_angle: Mapped[str | None] = mapped_column(Text, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True, index=True)
    actioned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    company = relationship("Company", backref="intent_signals")

    def is_active(self, now: datetime | None = None) -> bool:
        """Whether this signal is still within its buying window."""
        if self.expires_at is None:
            return True
        if now is None:
            now = datetime.now(timezone.utc)
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp >= now

    @property
    def combined_score(self) -> float:
        """Relevance-weighted urgency, 0-100. A highly relevant + urgent signal
        approaches 100; a relevant-but-not-urgent one sits in the middle."""
        return round((self.relevance_score * 0.6) + (self.urgency_score * 0.4), 1)

    def __repr__(self) -> str:
        return f"<IntentSignal {self.signal_type} company={self.company_id} rel={self.relevance_score} urg={self.urgency_score}>"
