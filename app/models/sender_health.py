"""Sender-account deliverability health (feature 3.3).

One row per sending account (Gmail or SMTP). Aggregates recent bounce /
complaint / open behaviour into a 0-100 reputation score and a
``throttle_factor`` (0..1) that the send limiter multiplies into that account's
daily cap. A healthy account rides at 1.0; a struggling one is throttled down so
volume shifts to healthier accounts automatically.
"""

import uuid
from datetime import datetime

from sqlalchemy import Float, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# Which transport the account belongs to. Not a DB enum so new transports don't
# need a migration.
SENDER_ACCOUNT_TYPES = ("gmail", "smtp", "ses")


class SenderHealth(Base):
    __tablename__ = "sender_health"
    __table_args__ = (
        UniqueConstraint("account_type", "account_id", name="uq_sender_health_account"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # account_id references gmail_accounts.id or smtp_accounts.id depending on
    # account_type. Deliberately NOT a FK (two possible parent tables), so the
    # row survives account deletion for historical analysis.
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    account_type: Mapped[str] = mapped_column(String(10), nullable=False, default="gmail")
    account_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    sent_7d: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bounces_7d: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    complaints_7d: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    opens_7d: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    bounce_rate_7d: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    complaint_rate_7d: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    open_rate_7d: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # 0-100 reputation. throttle_factor (0..1) scales the account's daily cap.
    reputation_score: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    throttle_factor: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="healthy")  # healthy|watch|throttled|critical
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    last_assessed: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<SenderHealth {self.account_type}:{self.account_email} "
            f"rep={self.reputation_score} throttle={self.throttle_factor}>"
        )
