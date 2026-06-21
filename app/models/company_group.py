"""Company Group model — named groups for organizing companies."""

import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Text, ForeignKey, Table, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# Association table for many-to-many
company_group_members = Table(
    "company_group_members",
    Base.metadata,
    Column("group_id", UUID(as_uuid=True), ForeignKey("company_groups.id", ondelete="CASCADE"), primary_key=True),
    Column("company_id", UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True),
)


class CompanyGroup(Base):
    __tablename__ = "company_groups"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    color: Mapped[str | None] = mapped_column(String(20), nullable=True, default="#3b82f6")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    companies = relationship("Company", secondary=company_group_members, backref="groups", lazy="selectin")
