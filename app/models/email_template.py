"""Email template and send-log models for outreach email composition."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class EmailTemplate(Base):
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


class EmailSend(Base):
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

    # Relationships
    template: Mapped["EmailTemplate | None"] = relationship("EmailTemplate", back_populates="sends")
    contact: Mapped["Contact | None"] = relationship("Contact")

    def __repr__(self) -> str:
        return f"<EmailSend to={self.to_email} status={self.status}>"
