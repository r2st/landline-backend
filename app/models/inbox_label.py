"""Inbox labels — user-created tags for organizing mail threads.

Two small side tables, both keyed to the human looking at the mailbox (like
``inbox_thread_state``), so labels never touch the shared ``email_sends`` /
``email_replies`` message rows and two users can tag the same conversation
independently:

* :class:`InboxLabel` — a named, coloured label a user created.
* :class:`InboxThreadLabel` — the many-to-many join attaching a label to a
  ``gmail_thread_id`` for that user.

Thread ids are plain indexed strings (not FKs) for the same reason as
``inbox_thread_state``: thread ids live on message rows and synthetic
``__solo_*`` ids exist for un-threaded messages, so a string keeps a label
attachable to any thread the inbox can show.
"""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import OrgScopedMixin


class InboxLabel(Base, OrgScopedMixin):
    """A user-created label (name + colour) for organizing inbox threads."""

    __tablename__ = "inbox_labels"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_inbox_label_user_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    # A short colour token the frontend maps to a chip palette (e.g. "indigo",
    # "green", "#ef4444"). Kept free-form so the palette can grow without a
    # schema change.
    color: Mapped[str] = mapped_column(
        String(24), nullable=False, default="gray", server_default="gray"
    )
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
        return f"<InboxLabel {self.name!r} user={self.user_id}>"


class InboxThreadLabel(Base, OrgScopedMixin):
    """Attaches one :class:`InboxLabel` to one thread for one user."""

    __tablename__ = "inbox_thread_labels"
    __table_args__ = (
        UniqueConstraint(
            "gmail_thread_id",
            "label_id",
            "user_id",
            name="uq_inbox_thread_label_thread_label_user",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    gmail_thread_id: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True
    )
    label_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("inbox_labels.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<InboxThreadLabel thread={self.gmail_thread_id} "
            f"label={self.label_id} user={self.user_id}>"
        )
