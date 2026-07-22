"""InboxThreadState — per-user archive / snooze / read state for a mail thread.

The unified Inbox groups messages by ``gmail_thread_id``. Archive, snooze and
read-override are *view* state that belong to the human looking at the mailbox,
not to the underlying message rows (which may be shared across a team). Keeping
them in a small side table keyed by ``(gmail_thread_id, user_id)`` avoids schema
churn on the two message tables (``email_sends`` / ``email_replies``) and lets
two users hold independent archive state on the same conversation.

See ``docs/INBOX_IMPROVEMENTS.md`` §5 — this is the recommended
``inbox_thread_state`` home for archive/snooze/read overrides.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import OrgScopedMixin


class InboxThreadState(Base, OrgScopedMixin):
    """A user's archive / snooze / read override for one mail thread."""

    __tablename__ = "inbox_thread_state"
    __table_args__ = (
        UniqueConstraint(
            "gmail_thread_id", "user_id", name="uq_inbox_thread_state_thread_user"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # The thread this state applies to. Not an FK — thread ids live on message
    # rows (and synthetic ``__solo_*`` ids exist for un-threaded messages), so a
    # plain indexed string keeps state attachable to any thread the inbox shows.
    gmail_thread_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Archived threads drop out of the default inbox view and surface under the
    # dedicated "Archived" tab.
    is_archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", index=True
    )
    # Snooze: hidden from the default view until this timestamp, then resurfaces.
    # (Column shipped now; the resurfacing sweep is P2-3, deferred.)
    snoozed_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    # Manual read override. NULL = follow the message rows' own ``is_read``.
    # True/False lets a user force a whole thread read/unread independent of the
    # per-message flags.
    read_override: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:
        return (
            f"<InboxThreadState thread={self.gmail_thread_id} user={self.user_id} "
            f"archived={self.is_archived}>"
        )
