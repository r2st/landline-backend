"""Company research model — AI-synthesized research brief for outreach (feature 1.4).

The AI Company Research Agent (``app.services.research_agent``) combines the
website scrape, Apollo enrichment, and recent news into a single actionable
brief: named pain points, ranked outreach angles, and competitive intel. The
brief feeds directly into email and call-script generation so every first touch
is grounded in what actually matters to the company.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class CompanyResearch(Base):
    __tablename__ = "company_research"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # One-paragraph narrative brief: who they are and why they're a fit.
    research_brief: Mapped[str | None] = mapped_column(Text)

    # Specific, evidence-backed operational pain points. Each item:
    # {"pain": "...", "evidence": "...", "how_we_help": "..."}
    pain_points: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")

    # Ranked outreach angles. Each item:
    # {"angle": "...", "reasoning": "...", "opening_line": "...", "rank": 1}
    outreach_angles: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")

    # Competitors / technologies mentioned that we compete with or integrate with.
    # {"competitors": [...], "technologies": [...], "notes": "..."}
    competitive_intel: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")

    # Which sources actually contributed (website / apollo / news), for auditing.
    sources_used: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")

    # Raw structured signals the synthesis was built from (scrape summary,
    # enrichment fields, news headlines) — kept so a re-synthesis can skip refetch.
    raw_signals: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")

    model_used: Mapped[str | None] = mapped_column(String(50))

    # Staleness: research goes stale as a company changes. The agent marks a
    # brief stale (or callers can) so it gets refreshed before the next outreach.
    is_stale: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    researched_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    company = relationship("Company", backref="research_briefs")

    def __repr__(self) -> str:
        return f"<CompanyResearch company={self.company_id} stale={self.is_stale}>"
