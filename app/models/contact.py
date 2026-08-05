"""Contact model — a person at a company."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
    inspect as sa_inspect,
)
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import OrgScopedMixin


class Contact(Base, OrgScopedMixin):
    __tablename__ = "contacts"
    __table_args__ = (
        # Every company detail page, every export and the whole enrolment path
        # loads a company's contacts by this FK; Postgres does not index a
        # foreign key for you. Created by migration 001 and declared here so the
        # model matches the database — see ``tests/test_index_declarations.py``
        # for why an index the database has and the model doesn't is dangerous
        # rather than merely untidy.
        Index("ix_contacts_company", "company_id"),
        # ``email`` is indexed raw by 001 and **nothing matches it raw**. The
        # column has no unique constraint and is not normalised on write, so one
        # human holds several rows with different casing; every lookup that is
        # about a recipient rather than a row compares ``lower(email)``, which a
        # b-tree on ``email`` cannot serve. Sixteen call sites — the compliance
        # gate on every send, the limiter's cap and gap checks, reply/bounce/
        # complaint attribution on their beats, and GDPR erasure — sequentially
        # scanned the fastest-growing table in the schema. Migration 119.
        # ``text()`` rather than ``func.lower(email)``: inside ``__table_args__``
        # the column object does not exist yet, and passing the *string*
        # ``"email"`` would index the SQL literal instead of the column.
        Index("ix_contacts_email_lower", text("lower(email)")),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    first_name: Mapped[str | None] = mapped_column(String(100), index=True)
    last_name: Mapped[str | None] = mapped_column(String(100), index=True)
    title: Mapped[str | None] = mapped_column(String(150))
    department: Mapped[str | None] = mapped_column(String(150))
    email: Mapped[str | None] = mapped_column(String(255), index=True)
    phone: Mapped[str | None] = mapped_column(String(30))
    mobile: Mapped[str | None] = mapped_column(String(30))
    fax: Mapped[str | None] = mapped_column(String(30))
    # Any phone numbers beyond phone/mobile/fax, as a list of {"label","number"}.
    # Ensures a card with several numbers never loses any of them.
    additional_phones: Mapped[list | None] = mapped_column(JSONB)
    # --- Phone enrichment for warm leads ---
    # Where phone/mobile came from: 'apollo' (people match), 'company' (the org's
    # main line, fallback), 'card' (scanned business card), or 'manual'. Lets the
    # UI badge an auto-found number and stops a sweep from clobbering a hand-edit.
    phone_source: Mapped[str | None] = mapped_column(String(20))
    # When the warm-lead sweep last *attempted* to find a phone. Set on every
    # attempt (hit or miss) so the sweep is idempotent and never re-charges the
    # enrichment API for the same contact. NULL = never attempted (the sweep's
    # selection set), mirroring ``email_verified_at``.
    phone_enriched_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), index=True
    )
    linkedin_url: Mapped[str | None] = mapped_column(Text)
    website: Mapped[str | None] = mapped_column(Text)
    # Full postal address captured from a scanned card (kept on the contact so the
    # detail view can show exactly what was on the card, independent of the company).
    address: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(100))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    country: Mapped[str | None] = mapped_column(String(100))
    notes: Mapped[str | None] = mapped_column(Text)
    # The scanned business-card image itself, stored as a base64 data URL
    # ("data:image/jpeg;base64,…") so it can be rendered later. Large, so the
    # column is deferred — it is only loaded (and serialized) by the single-contact
    # detail / card-image endpoints, never pulled in bulk by list queries.
    card_image: Mapped[str | None] = mapped_column(Text, deferred=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    unsubscribed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Hard compliance-suppression flag (single source of truth). Set when a reply
    # is classified as an explicit unsubscribe/opt-out; a suppressed contact is
    # filtered out of EVERY send path and is never emailed again. Distinct from
    # ``unsubscribed`` (link-based opt-out) so the two signals stay auditable.
    suppressed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", index=True
    )
    # General lifecycle marker. Feature 1 sets it to 'wrong_person' when a reply
    # says we've reached the wrong contact, so the list-cleanup view can find them.
    status: Mapped[str | None] = mapped_column(String(20))
    # IANA timezone name (e.g. "America/Los_Angeles"). When set, outreach to this
    # contact only sends inside the business-hours window of THEIR timezone
    # rather than the global default. NULL → use the global send window.
    timezone: Mapped[str | None] = mapped_column(String(64))
    # Bounce tracking for deliverability hygiene. A hard bounce (or
    # ``soft_bounce_suppress_threshold`` soft bounces) sets ``unsubscribed`` so
    # the dead address is never emailed again.
    bounce_count: Mapped[int] = mapped_column("bounce_count", default=0, server_default="0")
    # 'hard' | 'soft' | 'blocked' — see bounce_handler.BOUNCE_KINDS. 'blocked'
    # means the last rejection was about our sending reputation, not this
    # mailbox, so it stamps the type and leaves ``bounce_count`` alone.
    last_bounce_type: Mapped[str | None] = mapped_column(String(10))
    last_bounced_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    # Email deliverability pre-check (MX record validation). NULL = never checked.
    # 'valid' — domain has MX records; 'invalid' — no MX / bad syntax (never email);
    # 'unknown' — lookup could not complete (DNS down / dep missing), don't block.
    email_status: Mapped[str | None] = mapped_column(String(10))
    email_checked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    # --- Verification detail (P3). ``email_status`` above stays the only gate a
    # send path reads; everything here explains how it got that value. ---
    # Which layer produced the verdict: 'mx' (free syntax/DNS pre-check) or
    # 'hunter' (paid per-mailbox SMTP check). 'manual' when an operator overrides.
    email_verification_source: Mapped[str | None] = mapped_column(String(10))
    # Hunter's verdict: 'deliverable' | 'undeliverable' | 'risky' | 'unknown'.
    # 'risky' is sendable but never auto-enrollable — see hunter_verification.
    email_verification_result: Mapped[str | None] = mapped_column(String(15))
    email_verification_score: Mapped[int | None] = mapped_column(Integer)  # 0-100
    # {disposable, webmail, gibberish, accept_all, smtp_check, mx_records, raw}
    email_verification_meta: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default="{}"
    )
    # NULL = never verified. Set only once a layer reached a *verdict*, so a
    # Hunter outage leaves it NULL and the contact is retried, not written off.
    email_verified_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), index=True
    )
    # Free-form operator labels ("trade-show-2026", "warm-intro", "do-not-call").
    # Mirrors ``Company.tags``. Segmentation the taxonomy columns can't express:
    # ``status`` is a lifecycle marker with fixed meanings and ``contact_group``
    # is genuine-vs-test, so neither can carry "people I met at IMTS". Defaults to
    # [] rather than NULL so every read can iterate without a guard.
    tags: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    source: Mapped[str | None] = mapped_column(String(50), index=True)
    # 'genuine' (real business lead) or 'test' (internal/test data), or NULL if
    # unclassified. Lets the UI and outreach keep test contacts out of the way.
    contact_group: Mapped[str | None] = mapped_column(String(20), index=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    # GDPR right-to-erasure: set when the contact's PII has been scrubbed. A
    # non-null value means "anonymized" — the row is retained for referential
    # integrity but carries no personal data (see app/services/gdpr.py).
    anonymized_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    # Relationships
    company: Mapped["Company"] = relationship("Company", back_populates="contacts")
    consent_records: Mapped[list["ConsentRecord"]] = relationship(
        "ConsentRecord", back_populates="contact", cascade="all, delete-orphan", lazy="selectin"
    )
    enrollments: Mapped[list["OutreachEnrollment"]] = relationship(
        "OutreachEnrollment", back_populates="contact", cascade="all, delete-orphan", lazy="selectin"
    )
    interactions: Mapped[list["Interaction"]] = relationship(
        "Interaction", back_populates="contact", lazy="selectin"
    )

    @property
    def full_name(self) -> str:
        parts = [p for p in (self.first_name, self.last_name) if p]
        return " ".join(parts) or "Unknown"

    @property
    def has_card_image(self) -> bool:
        # card_image is a deferred column; don't trigger a per-row lazy load just
        # to answer this in list responses. If it hasn't been loaded, report False
        # (the detail endpoint force-loads it, so it's accurate there).
        if "card_image" in sa_inspect(self).unloaded:
            return False
        return bool(self.card_image)

    def __repr__(self) -> str:
        return f"<Contact {self.full_name} ({self.email})>"
