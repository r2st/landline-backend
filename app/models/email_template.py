"""Email template and send-log models for outreach email composition."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import OrgScopedMixin

# ---------------------------------------------------------------------------
# ``EmailSend.status`` is a LIFECYCLE column, not a terminal one
# ---------------------------------------------------------------------------
# This is the part that keeps being missed, so it is stated where the column is
# declared rather than in each of the six places that filter on it.
#
# A row starts ``pending``, becomes ``sent`` when the transport accepts it — and
# then **keeps moving**. ``app.routers.tracking`` advances it on every
# engagement signal, guarded by a rank ladder that only ever climbs:
# ``sent → opened → clicked → replied``. So the moment a recipient opens the
# message, that send's status is no longer ``"sent"``.
#
# Every counter written as ``status == "sent"`` therefore answers "how many
# emails did we send that nobody engaged with?" while reading as "how many
# emails did we send". The two agree exactly when the product isn't working,
# which is why the mistake survives review and why no test caught it: a fixture
# that sends and never opens cannot tell the two questions apart.
#
# The direction is the dangerous part. It is a metric that *shrinks* as
# engagement rises, and the counter it corrupts most is not a dashboard tile:
# ``send_limiter._db_sent_since`` is what the daily cap's Redis counter is
# seeded from. An undercounted seed hands back allowance the mailbox already
# spent — the one direction a cap must never be wrong in — and it hands back
# more of it the better the campaign is doing.

#: Statuses meaning the message reached the wire, so it consumed the mailbox's
#: sending allowance. ``bounced`` belongs here: a bounce is mail that went out
#: and was rejected afterwards, not mail that stayed home. Use this for send
#: counts, volume caps and per-company pacing.
WIRE_STATUSES = ("sent", "delivered", "opened", "clicked", "replied", "bounced")

#: Statuses meaning the message was also *delivered* — the denominator for an
#: engagement rate. ``bounced`` is excluded here and only here: counting a
#: bounce against an open rate reports a copy problem for what is a list
#: problem.
DELIVERED_STATUSES = tuple(s for s in WIRE_STATUSES if s != "bounced")

#: Statuses meaning nothing left the building. The complement of
#: ``WIRE_STATUSES``, kept explicit so a new status has to be classified by
#: whoever adds it rather than defaulting into whichever list is checked first.
#: ``skipped`` covers a missing address, a suppressed or unsubscribed recipient,
#: a spam-gate block, a warm-up hold, a spent daily cap and an engaged kill
#: switch — none of which put a message on the wire.
NON_WIRE_STATUSES = frozenset(
    {"pending", "scheduled", "skipped", "suppressed", "halted", "error", "failed"}
)


class EmailTemplate(Base, OrgScopedMixin):
    """A reusable outreach email template.

    The body is HTML and may contain merge fields rendered at send/preview time:
    {{first_name}}, {{company}}, {{industry_segment}}, {{city}}, {{contact_title}}.
    At most one template is marked ``is_default``; this is enforced in production
    by a partial unique index (see migration 002) and in application logic.
    """

    __tablename__ = "email_templates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Retired, but kept. Deleting a used template detaches every send it
    # produced (``email_sends.template_id`` is ON DELETE SET NULL), which throws
    # away the only record of which copy earned which replies — and the
    # templates worth retiring are precisely the ones with history. Archiving
    # takes it out of the pickers and leaves the attribution intact.
    is_archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    sends: Mapped[list["EmailSend"]] = relationship(
        "EmailSend", back_populates="template", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<EmailTemplate {self.name} default={self.is_default}>"


class EmailSend(Base, OrgScopedMixin):
    """A record of an outreach email composed and sent to a contact.

    Powers the "which contacts have been emailed and when" tracking view.
    """

    __tablename__ = "email_sends"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_templates.id", ondelete="SET NULL"), nullable=True
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    to_email: Mapped[str | None] = mapped_column(String(255), index=True)
    subject: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="sent", index=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    opened_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    clicked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    replied_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    ab_variant: Mapped[str | None] = mapped_column(String(1), nullable=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    # Gmail thread tracking for conversation view
    gmail_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gmail_thread_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    from_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    sender_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("gmail_accounts.id", ondelete="SET NULL"), nullable=True
    )
    # Which user sent this email (communication-history attribution). May be
    # null for system/automated sends; can also be derived via sender_account.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Relationships
    template: Mapped["EmailTemplate | None"] = relationship("EmailTemplate", back_populates="sends")
    contact: Mapped["Contact | None"] = relationship("Contact")
    sender_account: Mapped["GmailAccount | None"] = relationship("GmailAccount")
    user: Mapped["User | None"] = relationship("User")

    def __repr__(self) -> str:
        return f"<EmailSend to={self.to_email} status={self.status}>"
