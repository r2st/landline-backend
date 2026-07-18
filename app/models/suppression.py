"""Global do-not-contact suppression list (Feature 2).

One row per suppressed email address per org. Any outreach path consults this
list before putting mail on the wire (see
:func:`app.services.suppression.is_suppressed`), so a compliance upload of
unsubscribes / complaints / bounced addresses takes effect immediately and
across every sender and sequence.

Distinct from ``Contact.suppressed`` (a per-contact flag): a suppression entry
can exist for an address that isn't a contact at all — exactly the case a bulk
compliance import needs.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import OrgScopedMixin

# Why the address is suppressed.
SUPPRESSION_REASONS = ("unsubscribe", "bounce", "complaint", "manual")
# How the entry got here.
SUPPRESSION_SOURCES = ("import", "auto", "manual")


class SuppressionEntry(Base, OrgScopedMixin):
    __tablename__ = "suppression_entries"
    __table_args__ = (
        # One entry per address per org. Emails are normalised to lower-case on
        # write so casing can't sneak a duplicate (or a missed match) past this.
        UniqueConstraint("org_id", "email", name="uq_suppression_org_email"),
        CheckConstraint(
            "reason IN ('unsubscribe','bounce','complaint','manual')",
            name="ck_suppression_reason",
        ),
        CheckConstraint(
            "source IN ('import','auto','manual')",
            name="ck_suppression_source",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<SuppressionEntry {self.email} ({self.reason}/{self.source})>"
