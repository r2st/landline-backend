"""Relationship intelligence & org-chart mapping models (feature 2.3).

``StakeholderMap`` is the per-contact view of a deal's buying committee: what role
the contact plays (decision maker, champion, coach, blocker, end user), how strong
our relationship with them is, and where the engagement is trending.

``OrgRelationship`` captures inferred edges between two contacts at the same
company (reports_to / peers_with / manages) so the frontend can draw an org chart
and the multi-thread planner can reason about coverage gaps.
"""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import OrgScopedMixin

# Buying-committee roles the classifier assigns.
STAKEHOLDER_ROLES = ("decision_maker", "champion", "coach", "blocker", "end_user", "unknown")
# Relationship-strength buckets.
RELATIONSHIP_STRENGTHS = ("cold", "warm", "strong")
# Direction of engagement over time.
ENGAGEMENT_TRENDS = ("rising", "flat", "declining", "unknown")
# Edge types between two contacts.
ORG_RELATIONSHIP_TYPES = ("reports_to", "peers_with", "manages")


class StakeholderMap(Base, OrgScopedMixin):
    __tablename__ = "stakeholder_maps"
    __table_args__ = (
        UniqueConstraint("company_id", "contact_id", name="uq_stakeholder_company_contact"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    inferred_role: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    relationship_strength: Mapped[str] = mapped_column(String(10), nullable=False, default="cold")
    # 0-100 composite of email engagement + interaction recency/frequency.
    engagement_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    engagement_trend: Mapped[str] = mapped_column(String(12), nullable=False, default="unknown")
    last_interaction: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Full AI classification payload when the LLM enriched the row.
    ai_analysis: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<StakeholderMap contact={self.contact_id} role={self.inferred_role}>"


class OrgRelationship(Base, OrgScopedMixin):
    __tablename__ = "org_relationships"
    __table_args__ = (
        UniqueConstraint(
            "contact_id", "related_contact_id", "relationship_type",
            name="uq_org_edge",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    related_contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    relationship_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # 0-100 confidence in the inferred edge.
    confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    # How the edge was inferred: title_inference | cc_pattern | email_signature | meeting | manual
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="title_inference")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<OrgRelationship {self.contact_id} {self.relationship_type} {self.related_contact_id}>"
