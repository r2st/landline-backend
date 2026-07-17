"""Deal model — a revenue opportunity tied to a (post-conversion) company."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Deal(Base):
    __tablename__ = "deals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('won','lost','pending')",
            name="ck_deals_status",
        ),
        CheckConstraint(
            "deal_stage IN ('qualification','discovery','proposal','negotiation','closing')",
            name="ck_deals_deal_stage",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    # --- Sequence attribution (which outreach produced this deal) ---
    # The enrollment that first brought this company into the pipeline. Set at
    # deal-create time by tracing the company's contacts back to their earliest
    # enrollment (see deals.create_deal), or passed in explicitly. SET NULL on
    # enrollment delete so the deal — and its revenue — survives, it just loses
    # the finer-grained link. ``source_sequence_id`` is denormalized alongside so
    # attribution rollups don't have to join through enrollments (and so the
    # credited sequence is stable even if the enrollment is later pruned).
    source_enrollment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("outreach_enrollments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_sequence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("outreach_sequences.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[float | None] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    close_date: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    deal_stage: Mapped[str] = mapped_column(String(20), nullable=False, default="qualification")
    lost_reason: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    company: Mapped["Company"] = relationship("Company")

    def __repr__(self) -> str:
        return f"<Deal {self.title} {self.value} {self.currency} ({self.status})>"
