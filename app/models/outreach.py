"""Outreach enrollment and event models."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    desc,
    func,
)
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
        # Migration 001. The queue sweep filters on status on every pass. See
        # ``tests/test_index_declarations.py``.
        Index("ix_enrollment_status", "status"),
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

    # Why this enrollment is ``paused``, when the pause is one we intend to undo
    # by ourselves. Currently only ``'out_of_office'``: an autoresponder says the
    # contact is away, so the sequence stops and ``next_send_at`` carries the date
    # to resume on. The reason is what makes an automatic resume safe — every
    # other pause (no email address, bad domain, consent block, an operator's
    # decision) has a human or a different rule behind it and must never be
    # un-paused by a sweep looking for due dates. NULL = paused for one of those
    # reasons; leave it alone.
    pause_reason: Mapped[str | None] = mapped_column(String(30))

    # --- Send failure tracking (``services.send_failures``) ---
    #
    # Consecutive failed send *incidents*, not attempts: Celery retries one send
    # three times, and that is one failure here. Reset by any send that actually
    # goes out, so a non-zero value always means "currently failing", never
    # "failed once last March".
    #
    # A hard failure used to leave the enrollment untouched — ``active``, with a
    # due date in the past — so the queue sweep re-dispatched it every five
    # minutes forever. This counter is what makes repeated failure back off, and
    # eventually pause with ``pause_reason='send_failures'`` so a human can find
    # it. ``last_error`` is for that human; nothing parses it.
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    # --- At-most-once sending (``services.send_claim``) ---
    #
    # ``send_claim_step`` is the highest step number this enrollment has put on
    # the wire, claimed *before* the dispatcher call in its own committed
    # transaction — so it outlives a rollback of the send it describes. That
    # rollback is the whole point: everything the send records (the event, the
    # advanced step, the thread ids) commits *after* the message is already
    # gone, and a failure in that window restored an enrollment that still read
    # "step 1, active, due". Celery's retry then sent the same cold email to the
    # same person a second time.
    #
    # Not cleared on success. Claiming step 2 is a different value, so the
    # column can stay as the record of what shipped; it is cleared only when the
    # attempt is known not to have reached the wire (a soft skip, a dispatcher
    # exception raised before the transport).
    #
    # ``dispatched_at`` is a lease, not a claim: the queue sweep stamps it when
    # it queues a task and skips anything stamped inside the lease window. The
    # sweep marked nothing before, so an enrollment whose task was still in the
    # broker was due again five minutes later — a worker half an hour behind
    # accumulated six copies of every task in its backlog, each paying for
    # research and an LLM composition. It expires so a task lost with its broker
    # is re-dispatched rather than stranded.
    send_claim_step: Mapped[int | None] = mapped_column(Integer)
    send_claimed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

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
            # The three ``task_*`` types record a manual (non-email) touch: the
            # reminder was filed, a human did it, or nobody did it before the
            # hold expired. Kept out of ``sent`` on purpose — a filed reminder is
            # not a delivered message, and folding it in would put touches nobody
            # made into the denominator of every reply rate and into the bounce
            # rail's send count. See ``app.services.manual_touch``.
            "event_type IN ('sent','delivered','opened','clicked','replied',"
            "'bounced','unsubscribed','task_created','task_completed','task_expired')",
            name="ck_event_type",
        ),
        # The unified inbox reads sequence sends from here (this path records no
        # ``EmailSend`` row), scanning the most recent ``sent`` events for the
        # current org. Without this the query is a full scan plus a sort on
        # every inbox page load. Migration 101.
        Index(
            "ix_outreach_events_org_type_occurred",
            "org_id",
            "event_type",
            desc("occurred_at"),
        ),
        # Migration 001. This is the FK the enrolment's ``selectin`` event load
        # and every per-step engagement lookup join on, and Postgres does not
        # index a foreign key for you. See ``tests/test_index_declarations.py``.
        Index("ix_events_enrollment", "enrollment_id"),
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
