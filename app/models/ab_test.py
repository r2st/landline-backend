"""A/B testing persistence (Feature 3).

The statistical engine already lives in :mod:`app.services.ab_testing_service`
(z-test, confidence, bandit, ``auto_declare_winner``). These models add the
missing persistence layer around it:

* :class:`AbTestVariant` — up to 4 copy variants per sequence step. Variant ``A``
  is the control (the step's existing template); ``B``–``D`` are alternatives.
  A ``NULL`` template inherits the step's template.
* :class:`AbTestAssignment` — records which variant an enrollment was assigned at
  a given step, written *before* the send so a crash can't double-assign. The
  ``(enrollment_id, sequence_step_id)`` unique constraint makes assignment
  idempotent across retries / double-dispatch.
* :class:`AbTest` — the test-level config + state for one step under test
  (status, strategy, min sample, confidence target, metric, declared winner).

Per-variant metrics are **computed on demand** from ``outreach_events`` joined
to the assignment (``enrollment_id`` + step number) — counts are never
denormalised here, avoiding drift (the engine aggregates when asked).
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, Float, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# Allowed variant labels (up to 4 variants per step).
VARIANT_LABELS = ("A", "B", "C", "D")
# Assignment strategies.
AB_STRATEGIES = ("even", "bandit")
# Test lifecycle statuses.
AB_STATUSES = ("running", "insufficient_data", "completed")
# The success metric a test optimises toward (maps to an OutreachEvent type).
AB_METRICS = ("replied", "opened", "clicked")


class AbTest(Base):
    """One A/B test — one sequence step under test.

    Test-level config and state. Metrics are not stored here (computed from the
    send/engagement log). ``winner_variant_id`` + ``declared_at`` are set once a
    winner is declared (auto or manual override).
    """

    __tablename__ = "ab_tests"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    sequence_step_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sequence_steps.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="running", server_default="running"
    )
    # "even" (round-robin while learning) or "bandit" (epsilon/UCB1 favouring the
    # leader once data exists).
    strategy: Mapped[str] = mapped_column(
        String(20), nullable=False, default="even", server_default="even"
    )
    # Minimum sends per variant before a winner may be declared (guards against
    # calling a winner on noise). Defaults from calculate_required_sample_size.
    min_sample: Mapped[int] = mapped_column(
        Integer, nullable=False, default=200, server_default="200"
    )
    # Confidence to declare significance (default 95%).
    confidence_target: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.95, server_default="0.95"
    )
    # Primary success metric (reply rate default for cold outreach).
    metric: Mapped[str] = mapped_column(
        String(20), nullable=False, default="replied", server_default="replied"
    )
    winner_variant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ab_test_variants.id", ondelete="SET NULL")
    )
    declared_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<AbTest step={self.sequence_step_id} status={self.status}>"


class AbTestVariant(Base):
    """A single copy variant for a sequence step.

    ``subject_template`` / ``body_template`` override the step's templates; a
    ``NULL`` value inherits the step's own template (used for the control, which
    is typically the step's existing copy).
    """

    __tablename__ = "ab_test_variants"
    __table_args__ = (
        UniqueConstraint("sequence_step_id", "label", name="uq_variant_step_label"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    sequence_step_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sequence_steps.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    label: Mapped[str] = mapped_column(String(1), nullable=False)  # A | B | C | D
    subject_template: Mapped[str | None] = mapped_column(Text)  # None → inherit step
    body_template: Mapped[str | None] = mapped_column(Text)     # None → inherit step
    is_control: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<AbTestVariant step={self.sequence_step_id} label={self.label}>"


class AbTestAssignment(Base):
    """Which variant an enrollment got at a step — written before the send.

    The ``(enrollment_id, sequence_step_id)`` unique constraint guarantees an
    enrollment is assigned exactly one variant per step, so retries and
    double-dispatch reuse the same assignment (no double-count, consistent copy).
    """

    __tablename__ = "ab_test_assignments"
    __table_args__ = (
        UniqueConstraint(
            "enrollment_id", "sequence_step_id", name="uq_assignment_step"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    variant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ab_test_variants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence_step_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sequence_steps.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    enrollment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("outreach_enrollments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Links to the actual send row for metric attribution when a send path
    # produces an EmailSend. The sequence send path attributes via OutreachEvent
    # (enrollment_id + step number) instead, so this stays NULL there.
    email_send_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_sends.id", ondelete="SET NULL"), index=True
    )
    assigned_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<AbTestAssignment enrollment={self.enrollment_id} variant={self.variant_id}>"
