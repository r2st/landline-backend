"""Per-org LLM API key — lets a tenant bring their own Gemini (or other) key.

When set, AI features (tactics, scoring, news analysis, call scripts) use
the org's key instead of the platform default. The value is Fernet-encrypted
at rest (same ``TOKEN_ENCRYPTION_KEY`` as OAuth tokens and app credentials).

Org-scoped: each tenant manages their own key; the global read filter
ensures one org never sees another's.
"""

import uuid
from datetime import datetime

from sqlalchemy import String, Text, func
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import OrgScopedMixin


class OrgLlmKey(Base, OrgScopedMixin):
    __tablename__ = "org_llm_keys"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider: Mapped[str] = mapped_column(
        String(50), nullable=False, default="gemini"
    )
    key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    key_hint: Mapped[str] = mapped_column(
        String(20), nullable=False, default=""
    )
    updated_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
        server_default=func.now(), onupdate=func.now(),
    )

    def __repr__(self) -> str:
        return f"<OrgLlmKey provider={self.provider} org={self.org_id}>"
