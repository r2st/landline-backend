"""Connected Gmail account model — per-user OAuth sender identities.

Supports multiple connected accounts that can send and receive simultaneously.
Each account has a ``sending_enabled`` flag — when True, the account is
included in the sender rotation pool.  The ``is_active`` flag marks the
*primary* sender (used as default when no rotation logic applies).

Outreach mail is sent through Gmail (SPF/DKIM/DMARC aligned), falling back to
Amazon SES only when no sending-enabled Gmail account is connected.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class GmailAccount(Base):
    """A Gmail account connected via OAuth that can send outreach email."""

    __tablename__ = "gmail_accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    google_sub: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(255))

    # OAuth tokens — encrypted at rest with Fernet.
    refresh_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    access_token_encrypted: Mapped[str | None] = mapped_column(Text)
    token_expiry: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    scopes: Mapped[str | None] = mapped_column(Text)

    # Primary sender flag — exactly one account at a time.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Whether this account is in the sending rotation pool.
    sending_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # connected | revoked | error
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="connected")

    # Per-account daily send tracking for warm-up / throttling.
    daily_send_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    daily_count_reset_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    connected_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    # Relationships
    user: Mapped["User | None"] = relationship("User")

    def __repr__(self) -> str:
        return (
            f"<GmailAccount {self.email} active={self.is_active} "
            f"sending={self.sending_enabled} status={self.status}>"
        )
