"""Automated warm-up schedule for a sending account.

A brand-new mailbox (Gmail or SMTP) that dumps full outreach volume from a cold
IP/reputation gets flagged as spam. The industry practice is a graduated ramp:
send only a handful the first days and step the ceiling up over ~a month until
the account is "warmed".

One row per sending account tracks where it is on that ramp:

* ``current_day`` — 1-based day since ``start_date`` (day 1 == the start date).
* ``daily_limit`` — the cap for *today*, derived from the ramp for the current
  day (see :func:`app.services.warmup.limit_for_day`).
* ``status`` — ``warming`` while on the ramp, ``graduated`` once past day 30
  (the org's normal daily limit applies), ``paused`` to hold the account.

Like :class:`app.models.sender_health.SenderHealth`, the account link is a bare
``account_id`` + ``account_type`` pair rather than a real FK, because the parent
can be either ``gmail_accounts`` or ``smtp_accounts``. The uniqueness constraint
keeps it to one plan per account.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import Date, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import OrgScopedMixin

# Transports a warm-up plan can belong to (mirrors SenderHealth). Not a DB enum
# so a new transport doesn't need a migration.
WARMUP_ACCOUNT_TYPES = ("gmail", "smtp", "ses")

# Plan lifecycle states.
WARMUP_STATUSES = ("warming", "graduated", "paused")


class SenderWarmupPlan(Base, OrgScopedMixin):
    __tablename__ = "sender_warmup_plans"
    __table_args__ = (
        UniqueConstraint("account_type", "account_id", name="uq_sender_warmup_account"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # References gmail_accounts.id or smtp_accounts.id depending on account_type.
    # Deliberately NOT a FK (two possible parent tables).
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    account_type: Mapped[str] = mapped_column(String(10), nullable=False, default="gmail")
    account_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # The day the ramp began. current_day is derived from (today - start_date).
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    # 1-based day on the ramp (day 1 == start_date).
    current_day: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Cap for today, set from the ramp for current_day.
    daily_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    # warming | graduated | paused
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="warming")

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<SenderWarmupPlan {self.account_type}:{self.account_email} "
            f"day={self.current_day} limit={self.daily_limit} status={self.status}>"
        )
