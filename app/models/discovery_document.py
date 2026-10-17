"""A document uploaded on /discovery to find companies and contacts in.

The file bytes live in the row, like template attachments: the API runs on
the host and Celery in a container, so a path on one disk isn't readable from
the other. ``content`` is deferred so listing documents never loads the bytes,
and it is purged (set to NULL) 30 days after upload by
``app.tasks.discovery_documents.purge_expired_document_files`` — the extracted
results in ``extracted`` are kept.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, Integer, LargeBinary, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import OrgScopedMixin

STATUS_PARSING = "parsing"
STATUS_READY = "ready"
STATUS_FAILED = "failed"


class DiscoveryDocument(Base, OrgScopedMixin):
    __tablename__ = "discovery_documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    # Our own classification (csv | xlsx | pdf | docx | txt), not the client's.
    file_type: Mapped[str] = mapped_column(String(10), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True, deferred=True)
    content_purged_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=STATUS_PARSING)
    error: Mapped[Optional[str]] = mapped_column(Text)
    # {"candidates": [...], "stats": {...}} — see services.document_extract.
    extracted: Mapped[Optional[dict]] = mapped_column(JSONB)
    sequence_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("outreach_sequences.id", ondelete="SET NULL")
    )
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
        server_default=func.now(), onupdate=func.now(),
    )

    def __repr__(self) -> str:
        return f"<DiscoveryDocument {self.filename} {self.status}>"
