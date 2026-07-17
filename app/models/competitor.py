"""Competitive intelligence models — competitors and their detected mentions.

``Competitor`` is a tracked rival (a manufacturing-software vendor Apprend
competes with). It stores AI-generated battle-card content — strengths,
weaknesses, positioning, landmine questions — plus alias names used to detect
the competitor in free text.

``CompetitorMention`` records each time a competitor is detected in an email
reply, call note, or news article, linked to the company where it surfaced. The
stream of mentions powers deal-level competitive intel and win/loss correlation.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import OrgScopedMixin

MENTION_SOURCES = ("email", "call", "news", "manual")


class Competitor(Base, OrgScopedMixin):
    __tablename__ = "competitors"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Alternate names / product names to look for when detecting mentions in text.
    aliases: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    products: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    strengths: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    weaknesses: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    # Full battle card: {positioning, how_we_win, landmine_questions, talk_track, ...}
    battle_card: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_updated: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    mentions = relationship("CompetitorMention", back_populates="competitor", cascade="all, delete-orphan")

    def all_names(self) -> list[str]:
        """Every name/alias to match against, longest first (so multi-word
        aliases win over substrings)."""
        names = [self.name] + list(self.aliases or [])
        seen: set[str] = set()
        out: list[str] = []
        for n in names:
            if n and n.lower() not in seen:
                seen.add(n.lower())
                out.append(n)
        return sorted(out, key=len, reverse=True)

    def __repr__(self) -> str:
        return f"<Competitor {self.name}>"


class CompetitorMention(Base, OrgScopedMixin):
    __tablename__ = "competitor_mentions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    competitor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("competitors.id", ondelete="CASCADE"), nullable=False, index=True
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=True, index=True
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True
    )
    deal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("deals.id", ondelete="SET NULL"), nullable=True
    )
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="email", index=True)
    context: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Prospect stance toward the competitor, if discernible: evaluating|using|rejected|neutral
    sentiment: Mapped[str | None] = mapped_column(String(20), nullable=True)
    detected_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    competitor = relationship("Competitor", back_populates="mentions")
    company = relationship("Company")
    contact = relationship("Contact")

    def __repr__(self) -> str:
        return f"<CompetitorMention competitor={self.competitor_id} source={self.source}>"
