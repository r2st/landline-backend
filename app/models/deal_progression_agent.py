"""Deal Progression Agent models.

The agent watches the deal pipeline and, per organization, autonomously nudges
deals that have gone quiet: it drafts contextual follow-ups, suggests the next
best action, advances stages off real signals (a reply, a booked meeting), and
escalates the ones rotting past a hard threshold.

Two tables back it:

* :class:`DealProgressionAgent` — one config row per org (the knobs the agent
  runs within). ``org_id`` is UNIQUE so a tenant has exactly one agent.
* :class:`DealAction` — the append-only log of everything the agent proposed or
  did on a deal. It is both the human review queue (``status='pending'``) and
  the audit trail (``executed``/``dismissed``/``expired``).
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import OrgScopedMixin

# The five pipeline stages, in order. Advancing means moving right; the agent
# never moves a deal backwards. Mirrors ``deals.deal_stage``'s check constraint.
STAGE_ORDER = ("qualification", "discovery", "proposal", "negotiation", "closing")

# Sensible per-stage staleness defaults (days with no activity before a deal is
# considered stalled). Early stages should move fast; late stages get more room.
DEFAULT_STALE_THRESHOLDS = {
    "qualification": 5,
    "discovery": 3,
    "proposal": 5,
    "negotiation": 7,
    "closing": 7,
}

# The kinds of thing the agent records against a deal.
ACTION_TYPES = (
    "auto_followup",   # drafted a follow-up email (email_draft_id set)
    "suggestion",      # human-readable "do this next" recommendation
    "stage_change",    # advanced the deal's stage off a signal
    "escalation",      # notified the org manager about a rotting deal
    "at_risk_flag",    # flagged the deal as at risk and notified the owner
)

# Action lifecycle: the agent proposes → pending. A human (or, for autonomous
# orgs, the hourly executor) approves → approved. The executor then performs it
# (sends the follow-up, applies the advance) → executed. dismissed = rejected by
# a human; expired = the deal moved on (won/lost/advanced) before it was actioned.
ACTION_STATUSES = ("pending", "approved", "executed", "dismissed", "expired")


class DealProgressionAgent(Base, OrgScopedMixin):
    """Per-org configuration for the Deal Progression Agent."""

    __tablename__ = "deal_progression_agents"
    __table_args__ = (
        # One agent config per organization.
        UniqueConstraint("org_id", name="uq_deal_progression_agent_org"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # {"discovery": 3, "proposal": 5, ...} — days-with-no-activity per stage.
    stale_thresholds: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=lambda: dict(DEFAULT_STALE_THRESHOLDS),
        server_default="{}",
    )
    # When true the agent may advance a deal's stage off signals without a human.
    auto_advance_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # When true the agent drafts follow-ups for stalled deals (still queued for
    # review unless the org auto-approves them elsewhere).
    auto_followup_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    max_auto_followups_per_deal: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    # Days a deal may sit stalled before the agent escalates it to the manager.
    escalation_threshold_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=14, server_default="14"
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:
        return f"<DealProgressionAgent org={self.org_id} active={self.is_active}>"


class DealAction(Base, OrgScopedMixin):
    """A single thing the agent proposed or did on a deal."""

    __tablename__ = "deal_actions"
    __table_args__ = (
        CheckConstraint(
            "action_type IN ('auto_followup','suggestion','stage_change','escalation','at_risk_flag')",
            name="ck_deal_actions_action_type",
        ),
        CheckConstraint(
            "status IN ('pending','approved','executed','dismissed','expired')",
            name="ck_deal_actions_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    deal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("deals.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    action_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending",
        index=True,
    )
    # Why the agent recommended this — surfaced to the reviewer verbatim.
    ai_reasoning: Mapped[str | None] = mapped_column(Text)
    # Optional link to an AiEmailDraft when the follow-up was routed through the
    # sequence-review queue. SET NULL so pruning a draft doesn't erase history.
    email_draft_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_email_drafts.id", ondelete="SET NULL"),
        nullable=True,
    )
    # The drafted follow-up copy, stored on the action itself so the hourly
    # executor can send it without depending on a sequence enrollment (deal
    # follow-ups aren't always tied to one). Populated for ``auto_followup``.
    email_subject: Mapped[str | None] = mapped_column(Text)
    email_body: Mapped[str | None] = mapped_column(Text)
    # Human-readable "do this next" text.
    suggested_action: Mapped[str | None] = mapped_column(Text)
    executed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    dismissed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    deal: Mapped["Deal"] = relationship("Deal")

    def __repr__(self) -> str:
        return f"<DealAction deal={self.deal_id} {self.action_type} {self.status}>"
