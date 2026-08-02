"""Outreach sequence and step models.

Enhanced with multi-channel cadence support from *Combo Prospecting* (Tony Hughes):
- **Triple**: Call + Voicemail + Email in under 2 minutes
- **Quad**: Triple + LinkedIn InMail
- **Quint**: Quad + Twitter/social DM

Sequence steps now carry a ``channel`` attribute so the cadence orchestrator
can coordinate cross-channel timing and enforce collision rules.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import OrgScopedMixin

# Channel types for multi-channel cadence (from combo-prospecting research)
STEP_CHANNELS = ("email", "call", "linkedin", "video", "direct_mail")

# Conditional branching gates a step can carry — see ``SequenceStep.send_condition``
# for what each one means and ``app.tasks.outreach.evaluate_step_condition`` for
# how it is evaluated. This tuple is the storage contract the API validates
# against; the two are pinned to each other by a test.
SEND_CONDITIONS = (
    "always",
    "if_not_opened",
    "if_opened_no_reply",
    "if_clicked_no_reply",
    "if_not_clicked",
)

# ``cadence_pattern`` used to live here — a String(20) defaulting to
# "single_channel", carrying the Combo Prospecting taxonomy (triple / quad /
# quint). It was removed in migration 108, for two reasons that are worth
# keeping written down so it doesn't come back:
#
# 1. **Nothing read it and nothing wrote it.** No endpoint accepted it, no
#    service branched on it. Every row in the database held the column default,
#    so it carried no information — while looking, to anyone reading the schema
#    or the public API, like a switch that configured how a sequence ran.
# 2. **Its vocabulary could not describe the data model.** The taxonomy counts
#    call + voicemail + email + LinkedIn + social; the step model has email,
#    call, linkedin, video, direct_mail — no voicemail, no social — and has no
#    name at all for the two-channel cadence it can perfectly well express.
#
# The channel mix is a property of the steps, and the steps already know it.
# ``channel_mix()`` below reads it from them, so it is true by construction
# rather than by somebody remembering to update a second field.


def channel_mix(steps) -> list[str]:
    """The distinct channels a sequence actually touches, in step order.

    Derived from the steps rather than stored: a sequence's channel mix changes
    the moment a step is added, and a copy of it on the parent row is a claim
    that has to be maintained. Order follows the cadence so the reader sees
    "email, call" for an email-then-call sequence and "call, email" for the
    reverse — which is the difference between a follow-up and a warm-up call.
    """
    seen: list[str] = []
    for step in sorted(steps or [], key=lambda s: getattr(s, "step_number", 0) or 0):
        channel = (getattr(step, "channel", None) or "email").strip().lower()
        if channel and channel not in seen:
            seen.append(channel)
    return seen

# Voice the AI copywriter writes this sequence in. A quality lever, not a
# routing one: nothing branches on tone, it only shapes the prompt, so an
# unknown value degrades to the default rather than breaking a send.
#
# Per-sequence because tone is a property of the audience, not the sender —
# a plant manager drowning in scrap reads "empathetic" as understanding and
# "urgent" as noise, while an ops VP evaluating vendors reads the reverse.
# Sequences are already seeded per industry vertical, so this is the seam
# where that judgement belongs.
#
# The prompt guidance for each of these lives in
# ``app.services.ai_email_generator.TONE_GUIDANCE``; the two vocabularies are
# pinned to each other by a test. This tuple is the storage contract.
SEQUENCE_TONES = ("friendly", "professional", "urgent", "empathetic", "conversational")
# Conversational is the house voice the system prompt already describes, so
# defaulting here changes nothing about how existing sequences read.
DEFAULT_SEQUENCE_TONE = "conversational"


class OutreachSequence(Base, OrgScopedMixin):
    __tablename__ = "outreach_sequences"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','paused','completed')",
            name="ck_sequence_status",
        ),
        CheckConstraint(
            "tone IN ('friendly','professional','urgent','empathetic','conversational')",
            name="ck_sequence_tone",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # Industry vertical key (see app.services.industry_verticals), e.g.
    # "injection_molding". NULL for general/custom sequences.
    industry: Mapped[str | None] = mapped_column(String(80))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    # Voice the AI copywriter writes this sequence in — see SEQUENCE_TONES.
    # NOT NULL with a server default: the generator reads this on every compose,
    # and a NULL would mean "no voice", which isn't a state that means anything.
    tone: Mapped[str] = mapped_column(
        String(20), nullable=False,
        default=DEFAULT_SEQUENCE_TONE, server_default=DEFAULT_SEQUENCE_TONE,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    # Relationships
    steps: Mapped[list["SequenceStep"]] = relationship(
        "SequenceStep", back_populates="sequence", cascade="all, delete-orphan",
        order_by="SequenceStep.step_number", lazy="selectin"
    )
    enrollments: Mapped[list["OutreachEnrollment"]] = relationship(
        "OutreachEnrollment", back_populates="sequence", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def channels(self) -> list[str]:
        """The distinct channels this cadence touches, read off its steps.

        Safe to serialize: ``steps`` is ``lazy="selectin"``, so it is populated
        by the same query that loaded the sequence.
        """
        return channel_mix(self.steps)

    def __repr__(self) -> str:
        return f"<OutreachSequence {self.name}>"


class SequenceStep(Base, OrgScopedMixin):
    __tablename__ = "sequence_steps"
    __table_args__ = (
        UniqueConstraint("sequence_id", "step_number", name="uq_sequence_step"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    sequence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("outreach_sequences.id", ondelete="CASCADE"), nullable=False
    )
    step_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # --- Cadence: how long after the previous touch this step goes out. ---
    # The wait is ``delay_days`` days plus ``delay_hours`` hours, measured from
    # the last send (or from enrollment, for step 1). Resolved in one place —
    # ``app.services.step_delay.due_at`` — which every scheduler goes through, so
    # the five sites that compute a due date cannot drift from each other.
    delay_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Sub-day granularity. A whole-day floor cannot express the cadences that
    # matter most: the same-afternoon nudge after a click, or a two-touch
    # opener spaced across a morning. Added to ``delay_days``, so the existing
    # day-granular steps keep behaving exactly as they did.
    delay_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Count the day portion in business days, skipping Sat/Sun.
    #
    # Without this the stated cadence isn't the delivered one. The send window
    # already refuses to put mail on the wire at the weekend, so a 3-day step
    # from Thursday, a 2-day step from Friday and a 3-day step from Friday all
    # collapse onto the same Monday — three different intended spacings, one
    # actual one. Counting in business days makes a "3-day follow-up" mean three
    # days the recipient was at work.
    delay_business_days: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Random spread added to the due time, in minutes (0 = off).
    #
    # A batch enrolled together is scheduled together: every enrollment gets an
    # identical offset and comes due in the same sweep. Per-mailbox pacing
    # (``services.send_pacing``) spaces what reaches the wire, but the schedule
    # underneath it is still a comb, and it is the schedule that decides who is
    # in today's batch at all. The jitter is derived from the enrollment id, not
    # drawn fresh, so recomputing a due date never moves it — see
    # ``step_delay.jitter_for``.
    delay_jitter_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # How long a non-email step waits for the human before the cadence gives up
    # on the touch and moves on. NULL = the org default
    # (``settings.manual_step_timeout_hours``). Per-step because the right
    # deadline is a property of the touch: a call queue worked over a few days
    # and a same-afternoon "ring them now" nudge are both legitimate, and one
    # global number cannot be both. Ignored for email steps.
    task_timeout_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    subject_template: Mapped[str] = mapped_column(Text, nullable=False)
    body_template: Mapped[str] = mapped_column(Text, nullable=False)
    # Channel for this step — enables multi-channel cadence orchestration.
    # "email" (default), "call", "linkedin", "video", "direct_mail"
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default="email")
    # Conditional branching gate evaluated against the PRIOR step's engagement
    # for the contact before this step sends:
    #   "always"             — send unconditionally (default; unchanged behaviour)
    #   "if_not_opened"      — only send if the prior step was not opened
    #   "if_opened_no_reply" — only send if the prior step was opened but not replied
    #   "if_clicked_no_reply"— only send if they clicked anywhere in this
    #                          enrollment and haven't replied
    #   "if_not_clicked"     — only send if they have never clicked
    # When the condition isn't met the step is skipped (advanced past, no send).
    # The click branches read a deliberate human act; the open branches read a
    # signal that Apple MPP and Gmail's image proxy both fire on their own. See
    # ``app.tasks.outreach.evaluate_step_condition``.
    send_condition: Mapped[str] = mapped_column(
        String(20), nullable=False, default="always", server_default="always"
    )

    # Relationships
    sequence: Mapped["OutreachSequence"] = relationship("OutreachSequence", back_populates="steps")

    def __repr__(self) -> str:
        return f"<SequenceStep seq={self.sequence_id} step={self.step_number}>"
