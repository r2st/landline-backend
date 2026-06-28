"""Knowledge-base models — industry contacts and editable reference documents.

These tables back the website's "knowledge" section. They replace the static
``data/industry_contacts.csv`` and the markdown files under ``docs/`` so the
data is queryable, editable from the UI, and covered by the database backup.
The original files remain in the repo as the version-controlled seed source
(see :mod:`app.services.knowledge_data`).
"""

import uuid
from datetime import datetime

from sqlalchemy import String, Text, func
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class IndustryContact(Base):
    """A single decision-maker contact at a target manufacturing company.

    One company usually has several rows (one per contact), matching the
    multi-threading approach documented in the contact strategy.
    """

    __tablename__ = "industry_contacts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    industry: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    company_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    website: Mapped[str | None] = mapped_column(String(500))
    location: Mapped[str | None] = mapped_column(String(255))
    employee_count: Mapped[str | None] = mapped_column(String(50))
    contact_name: Mapped[str | None] = mapped_column(String(255))
    contact_title: Mapped[str | None] = mapped_column(String(255))
    contact_email: Mapped[str | None] = mapped_column(String(320), index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<IndustryContact {self.contact_name} @ {self.company_name}>"


class KnowledgeDocument(Base):
    """An editable reference document (markdown), keyed by a stable slug.

    Holds the sales-psychology guidelines and contact-approach strategy that
    previously lived as markdown files, so they can be read and edited from the
    website.
    """

    __tablename__ = "knowledge_documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    key: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False, default="general", index=True)
    format: Mapped[str] = mapped_column(String(20), nullable=False, default="markdown")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<KnowledgeDocument {self.key} ({self.category})>"
