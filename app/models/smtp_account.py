"""SMTP sender account model — manually-configured email senders.

Complements :class:`~app.models.gmail_account.GmailAccount` for providers that
aren't connected via Google OAuth (Outlook/Office 365, custom domains, any host
that speaks SMTP).  An SMTP account stores host/port/credentials so Landline can
authenticate and send as that address.

Shares the same sender-pool semantics as Gmail accounts:

* ``is_active``        — the *primary* sender (at most one across all senders).
* ``sending_enabled``  — included in the round-robin rotation pool.
* ``status``           — ``connected`` once a login test succeeds, ``error`` if
                         a send or verification fails.

The SMTP password is encrypted at rest with the same Fernet key as Gmail tokens.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SmtpAccount(Base):
    """A manually-configured SMTP account that can send outreach email."""

    __tablename__ = "smtp_accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(255))

    # SMTP connection details.
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=587)
    username: Mapped[str] = mapped_column(String(320), nullable=False)
    password_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    # STARTTLS (port 587) vs implicit TLS/SSL (port 465). Exactly one is typical.
    use_tls: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    use_ssl: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Primary sender flag — at most one across Gmail + SMTP at a time.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Whether this account is in the sending rotation pool.
    sending_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # connected | error
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="connected")
    # Last connection/send error, surfaced in the settings UI for troubleshooting.
    last_error: Mapped[str | None] = mapped_column(Text)

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

    def __repr__(self) -> str:
        return (
            f"<SmtpAccount {self.email} host={self.host}:{self.port} "
            f"active={self.is_active} sending={self.sending_enabled} status={self.status}>"
        )
