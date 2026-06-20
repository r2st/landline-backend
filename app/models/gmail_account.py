"""Connected Gmail account model — per-user OAuth sender identities.

Landline has no separate login system; instead, each person connects their own
Google account via OAuth and that connection *is* their sender identity. When an
account is the ``is_active`` one, outreach mail is sent through Gmail as that
user (so it carries Google's SPF/DKIM/DMARC alignment and stays out of spam),
falling back to Amazon SES only when no Gmail account is connected.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, String, Text, func
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class GmailAccount(Base):
    """A Gmail account connected via OAuth that can send outreach email."""

    __tablename__ = "gmail_accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # The connected Gmail address — this is the visible "From" sender.
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    # Google's stable, opaque user id (the OIDC "sub" claim).
    google_sub: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(255))

    # OAuth tokens — encrypted at rest with Fernet (see app/services/crypto.py).
    refresh_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    access_token_encrypted: Mapped[str | None] = mapped_column(Text)
    token_expiry: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    scopes: Mapped[str | None] = mapped_column(Text)

    # Exactly one connected account is the active sender at a time. A partial
    # unique index (see migration) enforces "at most one active".
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # connected | revoked | error
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="connected")

    connected_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    def __repr__(self) -> str:
        return f"<GmailAccount {self.email} active={self.is_active} status={self.status}>"
