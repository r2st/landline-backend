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

from sqlalchemy import ForeignKey, String, Text, func
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
    processed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<BounceRecord {self.recipient_email} {self.bounce_kind}>"
