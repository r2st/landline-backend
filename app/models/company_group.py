"""Company Group model — named groups for organizing companies.

A group is one tenant's filing system. It was created by migration 008, two
years before multi-tenancy, and migration 078 — which put ``org_id`` on 71
tables — could not see it: the scoping is expressed by mixing in
:class:`OrgScopedMixin`, autogenerate compares the models to the schema, and a
model with no mixin has nothing to add. So ``company_groups`` stayed a single
global namespace shared by every customer on the platform.

That is a read *and* a write: ``GET /groups`` listed every org's groups, and the
group id it handed back was enough to rename, delete, or re-populate another
tenant's folder, because every one of those handlers looked the group up by
primary key alone. Auto-grouping was the quiet one — it asks "is there already a
group called 'Aerospace'?", found another tenant's, and filed this tenant's
companies into it.

The member *companies* were never exposed: ``Company`` is org-scoped and the
loader criteria injected by the global filter propagate into the relationship
load, so a foreign group came back reading ``member_count: 0``. The group row
itself, and full write access to it, is what leaked.
"""

import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Text, ForeignKey, Table, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import OrgScopedMixin

# Association table for many-to-many
company_group_members = Table(
    "company_group_members",
    Base.metadata,
    Column("group_id", UUID(as_uuid=True), ForeignKey("company_groups.id", ondelete="CASCADE"), primary_key=True),
    Column("company_id", UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True),
)


class CompanyGroup(OrgScopedMixin, Base):
    __tablename__ = "company_groups"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    color: Mapped[str | None] = mapped_column(String(20), nullable=True, default="#3b82f6")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    companies = relationship("Company", secondary=company_group_members, backref="groups", lazy="selectin")
