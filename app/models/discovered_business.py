"""Businesses surfaced by Google Places, staged before they become companies.

This is deliberately a **staging table, not a companies row**. Places returns
business contact data, never person contact data:

===========================  ==========  ==========================
Name, address, geo, category  Excellent   Company creation
Phone                         Good        Call tasks
Website                       Good        The bridge to an email
Rating / review count         Good        Weak proxy for legitimacy
Employee count, revenue       **Absent**  --
**Email addresses**           **Absent**  --
**Named people / titles**     **Absent**  --
===========================  ==========  ==========================

So a Places result is a *lead on a prospect*, not a prospect, and the chain to
an emailable contact (website -> Hunter domain-search / scrape -> verify) loses
volume at every hop. Staging them here means the ones that never make it that
far cost nothing but a row, and the ones that do are auditable on the way in.

``place_id`` is unique: it is the strongest identity signal Places offers, and
the only Places field Google's terms permit caching indefinitely. It is what
stops a weekly re-run of the same search from re-staging the same business.

Company-level dedup is **not** done here — that belongs to the one shared rule
in ``app/services/ingestion.py``. ``company_id`` records where a business
landed, which is what makes re-import idempotent.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import OrgScopedMixin

# Lifecycle. ``new`` -> ``enriched`` (an email was recovered) -> ``imported``
# (it became a company, and a contact if it had an email). ``skipped`` is a
# human or the pipeline saying "not worth importing" — kept, not deleted, so a
# re-run doesn't resurface it.
DISCOVERY_STATUSES = ("new", "enriched", "imported", "skipped")

# Where the email came from, when there is one. Distinguishing these matters:
# a Hunter domain-search hit carries a confidence score, a scraped mailto is a
# guess that still has to survive verification.
EMAIL_SOURCES = ("hunter", "scrape", "places")


class DiscoveredBusiness(Base, OrgScopedMixin):
    """One business found by a Places search."""

    __tablename__ = "discovered_businesses"
    __table_args__ = (
        CheckConstraint(
            "status IN ('new','enriched','imported','skipped')",
            name="ck_discovered_businesses_status",
        ),
        # The results list is always filtered by status, newest first.
        Index("ix_discovered_businesses_status_created", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Google's own ID. Unique — this is the dedup anchor for repeat runs.
    place_id: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True
    )
    business_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    address: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(100))
    # ISO-3166-1 alpha-2. Not constrained to the companies allowlist: staging a
    # business we can't yet import is fine and informative. The allowlist is
    # enforced at import, by the ingestion service, which rejects rather than
    # relabels.
    country: Mapped[str | None] = mapped_column(String(2), index=True)
    phone: Mapped[str | None] = mapped_column(String(30))
    website: Mapped[str | None] = mapped_column(Text)
    # Normalized from ``website``. The join key to Hunter and to an existing
    # company — a bare website URL is not comparable, a domain is.
    domain: Mapped[str | None] = mapped_column(String(255), index=True)
    # Absent from Places. Populated by the enrichment chain, if at all — the
    # single field that decides whether this ever becomes an emailable contact.
    email: Mapped[str | None] = mapped_column(String(255))
    email_source: Mapped[str | None] = mapped_column(String(10))
    # Hunter's 0-100 confidence, when the email came from Hunter.
    email_confidence: Mapped[int | None] = mapped_column(Integer)
    # The person behind ``email``, when the source named one. Hunter's
    # domain-search returns first_name/last_name/position alongside the address
    # and this pipeline used to discard all three — which is why 29 of 39
    # production contacts had no first name and every cold email opened "Hi
    # there," (audited 2026-07-24). Places itself never yields a person, so
    # Hunter is the only hop that can fill these; a scraped address stays
    # nameless and is honestly recorded as such.
    contact_first_name: Mapped[str | None] = mapped_column(String(100))
    contact_last_name: Mapped[str | None] = mapped_column(String(100))
    contact_title: Mapped[str | None] = mapped_column(String(150))
    # Places primaryType / the search's business_type, e.g. 'hardware_store'.
    category: Mapped[str | None] = mapped_column(String(100), index=True)
    rating: Mapped[float | None] = mapped_column(Float)
    review_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        String(10), nullable=False, default="new", server_default="new", index=True
    )
    # Why a business was skipped, or why an import failed. An operator-readable
    # reason, so 'skipped' is never a dead end with no explanation.
    status_reason: Mapped[str | None] = mapped_column(Text)
    # 'google_places' today. Named rather than assumed so a second local-business
    # source doesn't need a second table.
    discovered_via: Mapped[str] = mapped_column(
        String(30), nullable=False, default="google_places", server_default="google_places"
    )
    # The exact textQuery that surfaced this, for credit post-mortems: "which
    # query produced businesses that became customers?"
    search_query: Mapped[str | None] = mapped_column(String(500))
    search_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("discovery_searches.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Set once imported. Makes re-import idempotent and gives the funnel its
    # join from a Places result through to a real pipeline company.
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Minimal Places payload: rating, types, formattedAddress. Google's terms
    # restrict caching Places *content* (place IDs are the documented
    # exception), so this stays a small explanatory digest of the fields we
    # actually masked for — not a full response dump.
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    discovered_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    imported_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:
        return f"<DiscoveredBusiness {self.business_name!r} {self.status}>"
