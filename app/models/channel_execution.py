"""Multi-channel step execution tracking (P2 audit — multichannel execution).

The orchestrator (feature 2.2) *decides* which channel to touch a lead on, and
``SequenceStep.channel`` lets a cadence be authored across email / LinkedIn /
phone — but until now only **email** actually executed. A
:class:`ChannelStepExecution` is the record of one attempted touch on *any*
channel: what we sent, through which provider, and how it landed (queued, sent,
simulated, delivered, failed, replied).

Email executions run for real through the existing dispatch pipeline; LinkedIn
and phone/SMS run through provider **stubs** today (``status="simulated"``) so
the framework — dispatcher → per-channel handler → status/reply tracking — is in
place and a real API only has to fill in one handler.

Rows are org-scoped and optionally tied to an enrollment + contact so a lead's
cross-channel history reads back as one timeline.
"""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import OrgScopedMixin

# Channels a step can execute on. Mirrors ORCHESTRATION_CHANNELS plus SMS, which
# shares the phone handler (Twilio) but is a distinct action.
CHANNEL_EXECUTION_CHANNELS = ("email", "linkedin", "phone", "sms")

# Lifecycle of a single channel touch.
#   pending    — row created, not yet dispatched
#   sent       — really put on the wire (email)
#   simulated  — a stubbed channel "sent" it (LinkedIn / phone / SMS stubs)
#   delivered  — provider confirmed delivery (from a status check)
#   failed     — the provider/handler refused or errored
#   skipped    — a guard declined (kill switch, suppression, missing data)
#   replied    — a reply came back on this touch
CHANNEL_EXECUTION_STATUSES = (
    "pending",
    "sent",
    "simulated",
    "delivered",
    "failed",
    "skipped",
    "replied",
)


class ChannelStepExecution(Base, OrgScopedMixin):
    __tablename__ = "channel_step_executions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Optional links back to the cadence this touch belongs to. Nullable so a
    # one-off manual touch (or a test) can be recorded too.
    enrollment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("outreach_enrollments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    step_number: Mapped[int | None] = mapped_column(nullable=True)

    channel: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    # What was attempted on the channel: message | connection_request | call | sms.
    action: Mapped[str] = mapped_column(String(40), nullable=False, default="message")
    # Concrete transport/provider that handled it: gmail, smtp, ses, linkedin_stub,
    # twilio_stub, etc. Lets the UI distinguish a real send from a simulated one.
    provider: Mapped[str | None] = mapped_column(String(40), nullable=True)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", index=True
    )
    # Provider-side id (message id, call SID, connection id) for status checks.
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # What we sent (subject/body/message/script) and the provider's raw response.
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # A captured reply on this touch (if any), and any error string.
    reply: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    status_checked_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    def __repr__(self) -> str:
        return (
            f"<ChannelStepExecution {self.channel}/{self.action} "
            f"status={self.status} provider={self.provider}>"
        )
