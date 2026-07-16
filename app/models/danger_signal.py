"""Danger-signal trip log (Feature 2 — Auto-Pause on Danger Signals).

One row per *trip*: whenever the danger monitor auto-pauses sending because a
deliverability threshold was breached, it records exactly which metric fired,
for which scope (global or a single sending account), the observed value vs the
threshold, and a snapshot of the contributing sends for the post-mortem.

These rows are the audit trail behind the kill switch: the resume flow requires
an operator to acknowledge the most recent *unresolved* trip before sending can
be turned back on, and stamps ``resolved_at`` / ``resolved_by`` / ``resolve_note``
when they do. The Dialtone activity feed also surfaces each trip as an
``auto_paused`` event.

Deliberately NOT a per-account FK (``account_id`` may reference either
``gmail_accounts`` or ``smtp_accounts``, and the row must survive account
deletion for historical analysis) — mirrors ``sender_health``.
"""

import uuid
from datetime import datetime

from sqlalchemy import Float, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# Which metric tripped. Not a DB enum so new metrics don't need a migration.
DANGER_METRICS = ("bounce_rate", "complaint_rate", "hard_bounce_count")
# Whether the breach was measured across all sending (``global``) or one account.
DANGER_SCOPES = ("global", "account")
# What the monitor did about it.
DANGER_ACTIONS = ("auto_paused", "warned")


class DangerSignalEvent(Base):
    __tablename__ = "danger_signal_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    metric: Mapped[str] = mapped_column(String(30), nullable=False)
    scope: Mapped[str] = mapped_column(String(10), nullable=False, default="global")
    # References gmail_accounts.id or smtp_accounts.id (see module docstring).
    account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    account_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    window: Mapped[str] = mapped_column(String(10), nullable=False, default="24h")

    observed_value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    action_taken: Mapped[str] = mapped_column(String(20), nullable=False, default="auto_paused")
    # Contributing send ids, the full breach list, per-account breakdown, and the
    # active thresholds — everything the post-mortem needs.
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    triggered_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True, index=True
    )
    resolved_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    resolve_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<DangerSignalEvent {self.metric} {self.scope}:{self.account_email or 'all'} "
            f"obs={self.observed_value} thr={self.threshold} "
            f"{'resolved' if self.resolved_at else 'active'}>"
        )
