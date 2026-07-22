"""Ledger of complaint / feedback-loop (FBL) reports already ingested.

A spam complaint is the single strongest "stop mailing this person NOW" signal a
mailbox provider gives us, and — unlike a bounce — it is invisible in anything we
already ingest. Before this table nothing read abuse reports, so a recipient who
hit "report spam" kept receiving the whole sequence; the only complaint signal
was Google Postmaster's *aggregate* spam-rate, which never names the individual.

This ledger closes that loop the same way ``bounce_records`` does for bounces:

    find ARF/FBL report → parse the complained-about recipient → suppress them

One row per source message (``gmail_message_id`` unique) makes the sweep safe to
run repeatedly — re-processing the same report is a no-op instead of re-writing
suppression state — and doubles as the audit trail for what was suppressed and
why.
"""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import OrgScopedMixin


class ComplaintRecord(Base, OrgScopedMixin):
    __tablename__ = "complaint_records"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # The source report. Unique — the dedup key that makes a repeated sweep a
    # no-op rather than re-suppressing / re-auditing the same complaint.
    gmail_message_id: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True
    )
    gmail_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("gmail_accounts.id", ondelete="SET NULL")
    )
    # The address that complained (ARF Original-Rcpt-To / the recipient of the
    # complained-about message), lowercased.
    recipient_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    # ARF ``Feedback-Type`` — 'abuse' | 'fraud' | 'virus' | 'other' | 'unknown'.
    feedback_type: Mapped[str] = mapped_column(String(20), nullable=False, default="abuse")
    # Where the report came from (reporting domain / FBL provider), for auditing.
    source: Mapped[str | None] = mapped_column(String(255))
    # Raw report excerpt kept for auditing a classification after the fact.
    diagnostic: Mapped[str | None] = mapped_column(Text)
    # What record_complaint did with it (matched contacts, suppressions).
    result: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    processed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<ComplaintRecord {self.recipient_email} {self.feedback_type}>"
