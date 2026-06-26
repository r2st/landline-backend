"""EmailReply model — tracks incoming email replies from contacts."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class EmailReply(Base):
    """A reply received from a contact via Gmail.

    Links back to the original EmailSend (when matched) and to the contact
    and company for easy querying. Gmail thread IDs allow grouping sent and
    received messages into conversation threads.
    """

    __tablename__ = "email_replies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email_send_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_sends.id", ondelete="SET NULL"), nullable=True
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True
    )
    gmail_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("gmail_accounts.id", ondelete="SET NULL"), nullable=True
    )
    gmail_message_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    gmail_thread_id: Mapped[str | None] = mapped_column(String(255), index=True)
    from_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    to_email: Mapped[str | None] = mapped_column(String(320))
    subject: Mapped[str | None] = mapped_column(String(500))
    body_text: Mapped[str | None] = mapped_column(Text)
    body_html: Mapped[str | None] = mapped_column(Text)
    snippet: Mapped[str | None] = mapped_column(String(500))
    received_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    # Relationships
    email_send: Mapped["EmailSend | None"] = relationship("EmailSend")
    contact: Mapped["Contact | None"] = relationship("Contact")
    company: Mapped["Company | None"] = relationship("Company")
    gmail_account: Mapped["GmailAccount | None"] = relationship("GmailAccount")

    def __repr__(self) -> str:
        return f"<EmailReply from={self.from_email} thread={self.gmail_thread_id}>"
