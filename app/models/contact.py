"""Contact model — a person at a company."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, String, Text, func, inspect as sa_inspect
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    first_name: Mapped[str | None] = mapped_column(String(100))
    last_name: Mapped[str | None] = mapped_column(String(100))
    title: Mapped[str | None] = mapped_column(String(150))
    department: Mapped[str | None] = mapped_column(String(150))
    email: Mapped[str | None] = mapped_column(String(255), index=True)
    phone: Mapped[str | None] = mapped_column(String(30))
    mobile: Mapped[str | None] = mapped_column(String(30))
    fax: Mapped[str | None] = mapped_column(String(30))
    # Any phone numbers beyond phone/mobile/fax, as a list of {"label","number"}.
    # Ensures a card with several numbers never loses any of them.
    additional_phones: Mapped[list | None] = mapped_column(JSONB)
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
    source: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

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
