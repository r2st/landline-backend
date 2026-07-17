"""Investor-outreach models — investors, their interactions, and shared docs.

Three tables power the fundraising / investor-relations workflow, mirroring the
lead-gen side of the platform but pointed the other way — at people who put
capital *into* Apprend rather than buy from it:

* ``Investor`` — a VC / angel / firm we're raising from, with an investment
  thesis (focus sectors, stage, cheque size), contact details, and a fundraising
  pipeline ``status`` (researching → contacted → … → committed/passed).
* ``InvestorInteraction`` — a logged touch (email/call/meeting/linkedin), its
  direction, outcome, and the agreed next step.
* ``InvestorDocument`` — a fundraising document (pitch deck, financial model,
  term sheet, cap table, …). Tied to one investor, or general (``investor_id``
  NULL) for the master data-room copy.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import OrgScopedMixin

INVESTOR_STATUSES = (
    "researching",
    "contacted",
    "interested",
    "meeting_scheduled",
    "due_diligence",
    "committed",
    "passed",
)
INVESTMENT_STAGES = ("seed", "series-a", "series-b", "growth")
INVESTOR_INTERACTION_TYPES = ("email", "call", "meeting", "linkedin")
INVESTOR_INTERACTION_DIRECTIONS = ("inbound", "outbound")
INVESTOR_DOCUMENT_TYPES = (
    "pitch_deck",
    "financial_model",
    "term_sheet",
    "cap_table",
    "business_plan",
    "other",
)


class Investor(Base, OrgScopedMixin):
    __tablename__ = "investors"
    __table_args__ = (
        CheckConstraint(
            "status IN ('researching','contacted','interested','meeting_scheduled',"
            "'due_diligence','committed','passed')",
            name="ck_investors_status",
        ),
        CheckConstraint(
            "investment_stage IS NULL OR investment_stage IN "
            "('seed','series-a','series-b','growth')",
            name="ck_investors_stage",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    firm: Mapped[str | None] = mapped_column(String(255), index=True)
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    phone: Mapped[str | None] = mapped_column(String(50))
    linkedin: Mapped[str | None] = mapped_column(String(500))
    website: Mapped[str | None] = mapped_column(String(500))
    # Sectors they invest in, e.g. ["manufacturing", "b2b_saas", "ai"].
    investment_focus: Mapped[list] = mapped_column(JSONB, default=list)
    investment_stage: Mapped[str | None] = mapped_column(String(20), index=True)
    # Typical cheque range in paise (integer — avoids float rounding on money).
    typical_check_size_min: Mapped[int | None] = mapped_column(Integer)
    typical_check_size_max: Mapped[int | None] = mapped_column(Integer)
    location: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="researching", index=True
    )
    source: Mapped[str | None] = mapped_column(String(100))
    last_contacted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), index=True)

    # Relationships
    interactions: Mapped[list["InvestorInteraction"]] = relationship(
        "InvestorInteraction",
        back_populates="investor",
        cascade="all, delete-orphan",
    )
    documents: Mapped[list["InvestorDocument"]] = relationship(
        "InvestorDocument",
        back_populates="investor",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Investor {self.name} ({self.firm}) {self.status}>"


class InvestorInteraction(Base, OrgScopedMixin):
    __tablename__ = "investor_interactions"
    __table_args__ = (
        CheckConstraint(
            "type IN ('email','call','meeting','linkedin')",
            name="ck_investor_interactions_type",
        ),
        CheckConstraint(
            "direction IN ('inbound','outbound')",
            name="ck_investor_interactions_direction",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    investor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("investors.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(20), nullable=False, default="outbound")
    subject: Mapped[str | None] = mapped_column(String(500))
    content: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(Text)
    next_steps: Mapped[str | None] = mapped_column(Text)
    interaction_date: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    # Relationships
    investor: Mapped["Investor"] = relationship("Investor", back_populates="interactions")

    def __repr__(self) -> str:
        return f"<InvestorInteraction {self.type}/{self.direction} investor={self.investor_id}>"


class InvestorDocument(Base, OrgScopedMixin):
    __tablename__ = "investor_documents"
    __table_args__ = (
        CheckConstraint(
            "type IN ('pitch_deck','financial_model','term_sheet','cap_table',"
            "'business_plan','other')",
            name="ck_investor_documents_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Nullable: a NULL investor_id is a general / master data-room document.
    investor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("investors.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    type: Mapped[str] = mapped_column(String(30), nullable=False, default="other", index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str | None] = mapped_column(Text)  # markdown body
    file_path: Mapped[str | None] = mapped_column(String(1000))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    # Relationships
    investor: Mapped["Investor | None"] = relationship("Investor", back_populates="documents")

    def __repr__(self) -> str:
        return f"<InvestorDocument {self.type} v{self.version} '{self.title}'>"
