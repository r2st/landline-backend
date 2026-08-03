"""Ledger of bounce notifications already ingested from the mailbox.

``bounce_handler.record_bounce`` is **not** idempotent — it increments
``Contact.bounce_count`` and suppresses the contact once the soft-bounce
threshold is crossed. The ingestion sweep runs on a schedule over a rolling
lookback window, so without a record of what it has already seen it would
re-count the same bounce on every pass and suppress live contacts purely as an
artifact of polling frequency.

One row per source message (``gmail_message_id`` unique) is what makes the sweep
safe to run repeatedly. It doubles as the audit trail for what was classified
and why.
"""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import OrgScopedMixin


class BounceRecord(Base, OrgScopedMixin):
    __tablename__ = "bounce_records"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # The source notification. Unique — this is the dedup key that makes a
    # repeated sweep a no-op rather than a double-count.
    gmail_message_id: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True
    )
    gmail_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("gmail_accounts.id", ondelete="SET NULL")
    )
    # The address that bounced (DSN Final-Recipient), lowercased.
    recipient_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    # 'hard' | 'soft' | 'blocked', as classified by bounce_handler.classify_bounce
    # (``BOUNCE_KINDS``). 'blocked' is a policy rejection — a spam verdict, a
    # DMARC failure, our IP on a blocklist — and suppresses nothing, because
    # none of it was about the recipient. Migration 113 widened the CHECK.
    bounce_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    # DSN Status (e.g. "5.1.1") and Diagnostic-Code, kept for auditing a
    # classification after the fact.
    status_code: Mapped[str | None] = mapped_column(String(20))
    diagnostic: Mapped[str | None] = mapped_column(Text)
    # What record_bounce did with it (matched contacts, suppressions).
    result: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    # How many ``bounced`` OutreachEvents this notification produced.
    #
    # Load-bearing for the danger rail, not bookkeeping. ``DangerMonitor`` sums
    # three bounce populations, and two of them describe the *same* event: the
    # ingestion sweep writes this row and then calls ``record_bounce``, which
    # books a funnel event on the enrollment that sent. Counting both doubled
    # every ingested DSN that hit a live sequence — which is the ordinary case
    # for an autonomous outreach agent — so the effective auto-pause threshold
    # was half the configured one, on a metric whose global breach flips the
    # platform-wide kill switch.
    #
    # A count rather than a flag because one address can hold several contact
    # rows (``contacts.email`` has no unique constraint) and stop several
    # enrollments. Non-zero means "already counted as a funnel event"; the rail
    # counts only the zeroes, which are the bounces no sequence recorded —
    # manual sends, and addresses whose enrollments had already finished.
    funnel_events: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    processed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<BounceRecord {self.recipient_email} {self.bounce_kind}>"
