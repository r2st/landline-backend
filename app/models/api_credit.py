"""API credit accounting for paid external providers (Apollo today).

The autonomous prospector spends real money on every run. ``prospecting_daily_max``
caps *enrollments*, not *API calls* — a misconfigured ICP can source 25 candidates,
enroll none, and still burn credits every day with nothing to show for it.

``ApiCreditLedger`` is one row per external call. The budget gate sums it over a
window and refuses the *next* call once the budget is gone, so an exhausted
budget costs nothing. It is a **guardrail, not billing truth** — reconcile
against the provider's own dashboard.
"""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# Providers that draw down a paid credit budget.
CREDIT_PROVIDERS = ("apollo", "hunter", "google_places")
# Outcome of the call the row accounts for. A rate-limited or errored call is
# still recorded: it may well have cost a credit, and "we got 429s all day" must
# be distinguishable from "we found nothing".
CREDIT_STATUSES = ("ok", "rate_limited", "error")


class ApiCreditLedger(Base):
    """One row per external API call."""

    __tablename__ = "api_credit_ledger"
    __table_args__ = (
        # The budget-window sum is always (provider, created_at >= cutoff).
        Index("ix_api_credit_ledger_provider_created", "provider", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    # 'org_search' | 'people_search' | 'enrich' | ...
    operation: Mapped[str] = mapped_column(String(30), nullable=False)
    # Provider-reported cost when the response exposes one, else 1.
    credits: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("prospecting_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    icp_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("icp_profiles.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(15), nullable=False, default="ok")
    # Request digest, response meta, retry count — for credit post-mortems.
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    def __repr__(self) -> str:
        return (
            f"<ApiCreditLedger {self.provider}.{self.operation} "
            f"credits={self.credits} {self.status}>"
        )
