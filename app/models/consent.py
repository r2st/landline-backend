"""Consent record model — tracks CASL/CAN-SPAM consent for contacts."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ConsentRecord(Base):
    __tablename__ = "consent_records"
    __table_args__ = (
        CheckConstraint(
            "consent_type IN ('express', 'implied', 'none')",
            name="ck_consent_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    consent_type: Mapped[str] = mapped_column(String(20), nullable=False)
    basis: Mapped[str | None] = mapped_column(Text)
    evidence_url: Mapped[str | None] = mapped_column(Text)
    granted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    # Relationships
    contact: Mapped["Contact"] = relationship("Contact", back_populates="consent_records")

    @property
    def is_valid(self) -> bool:
        from datetime import datetime, timezone as tz
        if self.revoked_at is not None:
            return False
        if self.expires_at is not None and self.expires_at < datetime.now(tz.utc):
            return False
        return self.consent_type in ("express", "implied")

    def __repr__(self) -> str:
        return f"<ConsentRecord contact={self.contact_id} type={self.consent_type}>"
