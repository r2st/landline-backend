"""Company model — represents a potential manufacturing lead."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Integer, String, Text, ARRAY, func
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (
        CheckConstraint("country IN ('US', 'CA')", name="ck_companies_country"),
        CheckConstraint(
            "stage IN ('new','researched','scored','contacted','engaged','qualified','opportunity','customer','disqualified')",
            name="ck_companies_stage",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    domain: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    city: Mapped[str | None] = mapped_column(String(100))
    state_province: Mapped[str | None] = mapped_column(String(100))
    country: Mapped[str] = mapped_column(String(2), nullable=False, default="US")
    employee_count: Mapped[int | None] = mapped_column(Integer)
    naics_code: Mapped[str | None] = mapped_column(String(10))
    naics_description: Mapped[str | None] = mapped_column(String(255))
    industry_segment: Mapped[str | None] = mapped_column(String(100))
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    website: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(String(30))
    stage: Mapped[str] = mapped_column(String(20), nullable=False, default="new")
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    contacts: Mapped[list["Contact"]] = relationship(
        "Contact", back_populates="company", cascade="all, delete-orphan", lazy="selectin"
    )
    lead_scores: Mapped[list["LeadScore"]] = relationship(
        "LeadScore", back_populates="company", cascade="all, delete-orphan", lazy="selectin"
    )
    interactions: Mapped[list["Interaction"]] = relationship(
        "Interaction", back_populates="company", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<Company {self.name} ({self.domain})>"
