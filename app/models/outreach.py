"""Outreach enrollment and event models."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Float, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import OrgScopedMixin


class OutreachEnrollment(Base, OrgScopedMixin):
    __tablename__ = "outreach_enrollments"
    __table_args__ = (
        CheckConstraint(
            # 'pending_review' = an AI-composed step is waiting on a human. Kept
            # distinct from 'paused' (which already means no-email / bad-domain /
            # consent-block) because the outreach queue only dispatches 'active',
            # so "a reviewer needs to look at this" must be tellable apart from
            # "never send this".
            "status IN ('active','paused','completed','replied','bounced',"
            "'unsubscribed','pending_review')",
            name="ck_enrollment_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    sequence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("outreach_sequences.id", ondelete="CASCADE"), nullable=False
    )
    current_step: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="active")
    enrolled_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    # Threading: chain follow-up emails into the same Gmail thread
    gmail_thread_id: Mapped[str | None] = mapped_column(String(255))
    gmail_message_id: Mapped[str | None] = mapped_column(String(255))
    sender_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("gmail_accounts.id", ondelete="SET NULL")
    )
    next_send_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    # Set True when the global sending kill switch paused this enrollment (it was
    # ``active`` when sending was halted). Lets a subsequent re-enable resume
    # *only* the enrollments the kill switch paused, leaving ones paused for
    # other reasons (no email, consent, bounce) untouched.
    kill_switch_paused: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # AI-Optimized Sequence Engine (feature 1.2): per-enrollment record of the
    # optimizer's decisions and reasoning. Each entry is one decision dict
    # ({step, action, reasoning, ...}) appended by sequence_optimizer before a
    # send, so the sequence's AI behaviour is auditable and A/B-comparable.
    optimization_context: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")

    # Autonomous Multi-Channel Orchestration (feature 2.2). ``orchestration_mode``
    # controls who drives the cadence:
    #   manual     — the human/sequence runs it (default; no orchestrator touch).
    #   assisted   — orchestrator proposes the next touch but a human approves it.
    #   autonomous — orchestrator decides AND executes within policy guardrails.
    # ``orchestration_log`` is an append-only JSONB array of TouchDecision dicts
    # ({channel, action, reasoning, requires_human, ...}) so every autonomous
    # decision is auditable.
    orchestration_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="manual", server_default="manual"
    )
    orchestration_log: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")

    # Smart Send-Time Optimization (Feature 6). ``scheduled_send_at`` is the
    # smart-timing target for the CURRENTLY-due step: the send queue computes the
    # recipient's next optimal (local) window when a step comes due and holds the
    # send until then, capped by ``max_smart_delay`` so optimization never stalls
    # a sequence. ``timing_arm`` splits enrollments into an ``optimized`` arm
    # (per-recipient timing) and a ``control`` arm (naive next-window timing) so
    # the lift can be measured — assigned once, then stable.
    scheduled_send_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    timing_arm: Mapped[str | None] = mapped_column(String(20))  # 'optimized' | 'control'

    # Adaptive Step Delays (Feature 3). A per-enrollment multiplier applied to
    # each step's base ``delay_days`` when scheduling the next send. Engagement
    # shortens the cadence (an open/click/reply halves it, floored); silence after
    # a step's window extends it (×1.5). The effective delay is clamped to
    # [1, 14] days so the factor can never stall or spam a sequence. 1.0 = the
    # sequence's own static delays, unchanged.
    adaptive_delay_factor: Mapped[float] = mapped_column(
        Float, nullable=False, default=1.0, server_default="1.0"
    )

    # Relationships
    contact: Mapped["Contact"] = relationship("Contact", back_populates="enrollments")
    sequence: Mapped["OutreachSequence"] = relationship("OutreachSequence", back_populates="enrollments")
    events: Mapped[list["OutreachEvent"]] = relationship(
        "OutreachEvent", back_populates="enrollment", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<OutreachEnrollment contact={self.contact_id} status={self.status}>"


class OutreachEvent(Base, OrgScopedMixin):
    __tablename__ = "outreach_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('sent','delivered','opened','clicked','replied','bounced','unsubscribed')",
            name="ck_event_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    enrollment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("outreach_enrollments.id", ondelete="CASCADE"), nullable=False
    )
    step_number: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    event_metadata: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)

    # Relationships
    enrollment: Mapped["OutreachEnrollment"] = relationship("OutreachEnrollment", back_populates="events")

    def __repr__(self) -> str:
        return f"<OutreachEvent {self.event_type} step={self.step_number}>"
