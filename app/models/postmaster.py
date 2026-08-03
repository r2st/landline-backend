"""Google Postmaster Tools integration (P2 audit — Gmail complaint visibility).

Gmail is Landline's primary transport but exposes **no feedback loop (FBL)**, so
spam complaints against Gmail-delivered mail are invisible to the app. Google
Postmaster Tools is the only window into that signal — domain/IP reputation, the
user-reported spam rate, SPF/DKIM/DMARC pass rates, and delivery errors — and
these two tables land it:

* :class:`PostmasterAccount` — an OAuth connection (``postmaster.readonly``) to a
  Google account that has verified one or more domains in Postmaster Tools.
  Tokens are Fernet-encrypted at rest, mirroring :class:`GmailAccount`.
* :class:`PostmasterMetric` — one row per (domain, date) of pulled metrics,
  upserted by the daily task so the dashboard can trend reputation and spam rate
  over time.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import Date, Float, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import OrgScopedMixin

# Google's domain/IP reputation buckets, plus our "unknown" for un-pulled data.
POSTMASTER_REPUTATIONS = ("HIGH", "MEDIUM", "LOW", "BAD", "REPUTATION_CATEGORY_UNSPECIFIED")


class PostmasterAccount(Base, OrgScopedMixin):
    """OAuth connection to Google Postmaster Tools for one Google account.

    The ``email`` / ``google_sub`` uniques are **per organization**. Migration
    093 wrote them platform-wide, which made a Google identity connectable by
    exactly one tenant on the whole deployment — see migration 115 for what that
    did to the second tenant who tried.
    """

    __tablename__ = "postmaster_accounts"
    __table_args__ = (
        UniqueConstraint("org_id", "email", name="uq_postmaster_accounts_org_email"),
        UniqueConstraint(
            "org_id", "google_sub", name="uq_postmaster_accounts_org_google_sub"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    google_sub: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(255))

    # OAuth tokens — encrypted at rest with Fernet (see app.services.crypto).
    refresh_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    access_token_encrypted: Mapped[str | None] = mapped_column(Text)
    token_expiry: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    scopes: Mapped[str | None] = mapped_column(Text)

    # connected | revoked | error
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="connected")
    last_pulled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    connected_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User | None"] = relationship("User")

    def __repr__(self) -> str:
        return f"<PostmasterAccount {self.email} status={self.status}>"


class PostmasterMetric(Base, OrgScopedMixin):
    """One day of Postmaster Tools metrics for one verified domain."""

    __tablename__ = "postmaster_metrics"
    __table_args__ = (
        # Per organization. Two tenants monitoring the same domain each keep
        # their own daily row; platform-wide, the second one to pull collided
        # with a row it could not see. See migration 115.
        UniqueConstraint(
            "org_id", "domain", "metric_date", name="uq_postmaster_org_domain_date"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # The day these stats describe (Postmaster reports trailing daily windows).
    metric_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # Overall domain reputation bucket (HIGH/MEDIUM/LOW/BAD).
    domain_reputation: Mapped[str | None] = mapped_column(String(40))
    # Per-IP reputation breakdown: [{"reputation": "HIGH", "ipCount": 3}, ...].
    ip_reputations: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    # User-reported spam rate (0..1) — the FBL signal Gmail otherwise hides.
    spam_rate: Mapped[float | None] = mapped_column(Float)

    # Authentication pass rates (0..1).
    dkim_success_rate: Mapped[float | None] = mapped_column(Float)
    spf_success_rate: Mapped[float | None] = mapped_column(Float)
    dmarc_success_rate: Mapped[float | None] = mapped_column(Float)

    # TLS encryption ratios (0..1) inbound/outbound.
    inbound_encryption_ratio: Mapped[float | None] = mapped_column(Float)
    outbound_encryption_ratio: Mapped[float | None] = mapped_column(Float)

    # Delivery-error breakdown: [{"errorClass": "...", "errorRatio": 0.01}, ...].
    delivery_errors: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    # Full raw trafficStats payload, for anything the columns don't capture.
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<PostmasterMetric {self.domain} {self.metric_date} "
            f"rep={self.domain_reputation} spam={self.spam_rate}>"
        )
