"""Orchestration policy — guardrails for autonomous multi-channel outreach (2.2).

An ``OrchestrationPolicy`` bounds what the autonomous orchestrator is allowed to
do for a set of enrollments: how many touches per day / in total, how long to
wait between touches, which channels it may use, which channels a full cadence
must include before giving up, and which situations force a hand-off to a human.

Policies are either **global** (``user_id`` NULL — the default fallback used when
a rep has no policy of their own) or **per-user** (scoped to one rep). The
orchestrator resolves the most specific active policy, falling back to the global
one, and finally to a hardcoded conservative default when neither exists.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import OrgScopedMixin

# The channels the orchestrator understands. Kept in code (not a DB enum) so the
# escalation ladder and policy validation share one source of truth.
ORCHESTRATION_CHANNELS = ("email", "linkedin", "phone")

# Situations that always route a touch to a human rather than auto-executing.
DEFAULT_HUMAN_REVIEW_TRIGGERS = ("pricing_request", "complex_objection", "referral", "legal")


class OrchestrationPolicy(Base, OrgScopedMixin):
    __tablename__ = "orchestration_policies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # NULL user_id = global default policy. Otherwise this policy governs one rep.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False, default="Default policy")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # --- Cadence guardrails ---
    max_daily_touches: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    # Hard stop: once an enrollment has had this many touches, the orchestrator
    # gives up (marks it exhausted) rather than pestering forever.
    max_total_touches: Mapped[int] = mapped_column(Integer, nullable=False, default=12)
    min_hours_between_touches: Mapped[int] = mapped_column(Integer, nullable=False, default=24)

    # --- Channel guardrails ---
    # Channels the orchestrator may use at all.
    allowed_channels: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=lambda: list(ORCHESTRATION_CHANNELS), server_default='["email", "linkedin", "phone"]'
    )
    # Channels a full cadence should attempt before the orchestrator concludes
    # the lead is unreachable (drives escalation).
    required_channels: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )

    # --- Human-in-the-loop ---
    # Signal keys that force a flag-for-human decision (see DEFAULT_HUMAN_REVIEW_TRIGGERS).
    human_review_triggers: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=lambda: list(DEFAULT_HUMAN_REVIEW_TRIGGERS),
        server_default='["pricing_request", "complex_objection", "referral", "legal"]',
    )

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        scope = "global" if self.user_id is None else f"user={self.user_id}"
        return f"<OrchestrationPolicy {self.name} ({scope})>"
