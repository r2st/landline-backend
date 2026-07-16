"""Autonomous prospecting models (feature 3.1).

Three tables power the autonomous prospecting agent:

* ``IcpProfile`` — a learned Ideal Customer Profile. Its weights/ranges are
  derived from the characteristics of *won* deals so the definition of a "good
  fit" evolves as the business wins more business. Kept as JSONB blobs (not
  rigid columns) so the learner can add new signal dimensions without a
  migration.
* ``ProspectingRun`` — one autonomous prospecting session: which sources were
  queried, how many companies were found / qualified / enrolled / queued for
  human review, and the run's outcome.
* ``ProspectCandidate`` — a single company surfaced by a run. High-confidence
  candidates are auto-enrolled; borderline ones land in a human review queue
  (``status == 'review'``) for a rep to approve or reject.
"""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# A prospecting run moves through these states.
PROSPECTING_RUN_STATUSES = ("running", "completed", "failed", "paused")
# A candidate's disposition within (or after) a run.
CANDIDATE_STATUSES = ("qualified", "review", "enrolled", "rejected", "skipped")


class IcpProfile(Base):
    """A learned Ideal Customer Profile.

    Only one profile is ``is_active`` at a time; the prospector uses the active
    one. ``learned_from_deal_count`` records how many won deals the current
    weights were fit on, so a stale profile can be re-learned.
    """

    __tablename__ = "icp_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False, default="Learned ICP")
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True, index=True)
    # industry_segment / naics -> weight (0-1). Higher = better fit.
    industry_weights: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # {"min": int|None, "max": int|None, "ideal": int|None}
    size_range: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # {"countries": [...], "states": [...]} with implicit preference order.
    location_preferences: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Free-form technology / systems signals that correlate with wins.
    technology_signals: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # Pain-point phrases the AI should look for when qualifying.
    pain_point_indicators: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # Hard disqualifiers: {"industries": [...], "countries": [...], "max_employees": int|None}
    exclusion_criteria: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    learned_from_deal_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model_used: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<IcpProfile {self.name} active={self.is_active} deals={self.learned_from_deal_count}>"


class ProspectingRun(Base):
    __tablename__ = "prospecting_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    icp_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("icp_profiles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    trigger: Mapped[str] = mapped_column(String(20), nullable=False, default="scheduled")  # scheduled|manual
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running", index=True)
    target_volume: Mapped[int] = mapped_column(Integer, nullable=False, default=25)
    sources_queried: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    companies_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    companies_qualified: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    companies_enrolled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    companies_review_queue: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Per-candidate summaries + guardrail notes for auditability.
    results: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    candidates: Mapped[list["ProspectCandidate"]] = relationship(
        "ProspectCandidate", back_populates="run", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return (
            f"<ProspectingRun {self.status} found={self.companies_found} "
            f"qualified={self.companies_qualified} enrolled={self.companies_enrolled}>"
        )


class ProspectCandidate(Base):
    """A company surfaced by a prospecting run.

    Links to a created ``Company`` row (``company_id``) once persisted. Borderline
    candidates sit in ``status == 'review'`` awaiting a human decision.
    """

    __tablename__ = "prospect_candidates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prospecting_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True
    )
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="unknown")
    raw_data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # 0-100 fit score against the ICP.
    fit_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    qualification: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="qualified", index=True)
    # How confident the unified ingestion dedup was when it matched (or didn't
    # match) this candidate to an existing company: 'certain' | 'high' |
    # 'medium' | 'none'. Anything below 'certain' vetoes auto-enroll — merging
    # two real prospects is worse than a trip through the review queue.
    dedup_confidence: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Set when a human confirms a medium-confidence match, recording which
    # company this candidate was folded into.
    merged_into_company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    run: Mapped["ProspectingRun"] = relationship("ProspectingRun", back_populates="candidates")

    def __repr__(self) -> str:
        return f"<ProspectCandidate {self.company_name} fit={self.fit_score} {self.status}>"
