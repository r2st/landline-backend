"""Email signature model for outreach email composition."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Index, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import OrgScopedMixin


class EmailSignature(Base, OrgScopedMixin):
    """A reusable email signature appended to outreach emails.

    At most one signature **per organization** is marked ``is_default``, and
    names are unique **within** an organization. Both were platform-wide until
    migration 114 — see the ``__table_args__`` note below.
    """

    __tablename__ = "email_signatures"

    # Migration 019 predates multi-tenancy, so it wrote both of these without an
    # org: ``UNIQUE (name)`` and ``UNIQUE (is_default) WHERE is_default``.
    # Migration 078 added ``org_id`` to this table and did not widen either —
    # autogenerate diffs models against the schema, and the model declared the
    # constraints exactly as the database already had them, so there was nothing
    # to report.
    #
    # A platform-wide ``UNIQUE (is_default) WHERE is_default`` permits **one
    # default signature across every tenant on the deployment**, and 019 seeds
    # it into the first-party org. So the second organization to exist could not
    # have one: ``create_email_signature`` auto-defaults a tenant's first
    # signature (their org-scoped read correctly finds no default), the insert
    # collided with a row in a *different* tenant that the reader cannot see, and
    # the handler reported it as "A signature with that name already exists".
    # That is every new tenant, on their first signature, with an error naming
    # the wrong cause.
    #
    # It is quieter than an error on the way out: ``email_dispatch`` appends
    # ``is_default`` signature to outbound mail, so a tenant that worked around
    # the 409 by leaving the flag off sent unsigned mail with nothing logged.
    #
    # ``UNIQUE (name)`` was the same shape one step milder — a tenant could not
    # name a signature "Default" if any other tenant had, and the 409 confirmed
    # the name existed somewhere, which is a cross-tenant read.
    __table_args__ = (
        UniqueConstraint("org_id", "name", name="uq_email_signatures_org_name"),
        Index(
            "uq_email_signatures_org_one_default",
            "org_id",
            "is_default",
            unique=True,
            postgresql_where=text("is_default"),
            sqlite_where=text("is_default"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    content_html: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<EmailSignature {self.name} default={self.is_default}>"
