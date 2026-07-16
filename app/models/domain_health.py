"""Domain health monitoring (Feature 5 — Domain Health Dashboard).

One row per sending **domain** (deduplicated across every Gmail/SMTP sending
account). Persists the latest SPF/DKIM/DMARC authentication verdicts and DNSBL
blacklist results, rolled up into a composite 0-100 ``health_score`` and a
``band`` (``healthy`` / ``watch`` / ``at_risk``).

The record is *upserted* per domain (unique on ``domain``) by the daily
``check_domain_health`` task. ``previous_score`` carries the prior run's score so
the service can alert only on a **change** (score/band transition or a new
blacklist listing) rather than on every daily run — avoiding alert fatigue.

Everything is fail-open: unverifiable components (DNS timeout, ``dnspython``
missing) are stored with ``status="unknown"`` and excluded from the score
denominator, so the dashboard reflects only what we could actually verify.
"""

import uuid
from datetime import datetime

from sqlalchemy import Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# Score → band boundaries. Mirrored by domain_health_service.band_for().
DOMAIN_HEALTH_BANDS = ("healthy", "watch", "at_risk")


class DomainHealth(Base):
    __tablename__ = "domain_health"
    __table_args__ = (
        UniqueConstraint("domain", name="uq_domain_health_domain"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # Raw check outputs (shape matches domain_auth.check_spf/dkim/dmarc and
    # blacklist_check.check_blacklists).
    spf: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    dkim: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    dmarc: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    blacklists: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Composite 0-100 score over the verifiable components, and its band.
    health_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    band: Mapped[str] = mapped_column(String(20), nullable=False, default="at_risk")
    # Which components were verifiable (contributed to the score), with weights.
    coverage: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    last_checked: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    # Prior run's score — the anchor for change-only alerting.
    previous_score: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<DomainHealth {self.domain} score={self.health_score} band={self.band}>"
