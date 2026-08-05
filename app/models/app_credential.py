"""Platform-wide API credentials, stored encrypted and editable from the UI.

Every credential the app needs — the Google OAuth client, the LLM provider
keys, Apollo — arrived only through the environment, so rotating one meant SSH
onto the box, editing ``.env.production``, and restarting the stack. That is a
deploy for a value change, and it is the reason a closed Google Cloud project
can take Gmail connectivity down until someone with SSH is available.

A row here **overrides** the environment for its key; no row means the
environment value stands (see :mod:`app.services.credential_store`). So this
table is a set of deltas, not a replacement config: a fresh install with an
empty table behaves exactly as it does today.

**Not org-scoped, deliberately.** These identify the *deployment* to a third
party, not a tenant to us: one Google Cloud project, one registered redirect
URI, one set of provider accounts. Two consequences follow, and both are the
reason it must not be `OrgScopedMixin`:

- the OAuth callback resolves the client secret with **no org bound** — it runs
  before any user context exists — as do the beat tasks;
- letting a tenant admin write the platform's Google client credentials would
  let them point the consent flow at a client they control. Only
  ``platform_admin`` may read or write this table.

Values are encrypted at rest with the same Fernet key as the OAuth refresh
tokens (``app.services.crypto``), because the column holds live client secrets
and provider keys — a database dump must not be a credential dump.
"""

import uuid
from datetime import datetime

from sqlalchemy import String, Text, func
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AppCredential(Base):
    __tablename__ = "app_credentials"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # A key from ``credential_store.MANAGED_CREDENTIALS`` — the registry is an
    # allowlist, so this table can never become an arbitrary settings-write
    # primitive for whoever reaches the endpoint.
    key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    value_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    # Email of the platform admin who last set it. Rotating a credential is the
    # kind of change someone has to be able to ask "who did this, and when?" of.
    updated_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
        server_default=func.now(), onupdate=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - never print the value
        return f"<AppCredential {self.key}>"
