"""Give company_groups an org, because it was the last shared table.

``company_groups`` is from migration 008. Migration 078 added ``org_id`` to 71
tables and skipped this one — scoping is declared by mixing
:class:`app.models.mixins.OrgScopedMixin` into a model, autogenerate diffs the
models against the schema, and a model without the mixin presents nothing to
add. It was the only tenant-owned table left outside the isolation machinery
(``Organization`` and ``TenantUsage`` are deliberately global; ``User`` is
scoped by hand where it needs to be).

Every group every customer had created therefore sat in one namespace: listed
to all of them, and renameable, deletable and re-populatable by all of them,
because each handler resolved the group by primary key with no further check.

Backfill and the membership rows
--------------------------------
Existing groups are stamped with the first-party org, which is the same choice
078 made and correct for the same reason: this deployment's data is Apprend's.

``company_group_members`` is a plain association table with no ``org_id`` of its
own and it does not need one — a membership is only reachable through its group,
and from here a group belongs to exactly one org. What it *can* hold is rows
written while the namespace was shared: a company in org A filed into a group
that this migration is about to award to org B. Those are deleted. They are
unreachable after the backfill anyway (the read filter drops the company from
the relationship load, which is why a foreign group already reported
``member_count: 0``), so this removes rows that were already invisible rather
than removing anything an operator can see today.

Revision ID: 111_company_groups_org_id
Revises: 110_suppression_erasure_reason
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "111_company_groups_org_id"
down_revision = "110_suppression_erasure_reason"
branch_labels = None
depends_on = None

# Same literal 078 used — it is the id 078 *inserted* into ``organizations``,
# not a placeholder. Kept inline rather than imported from app code so the
# migration keeps describing the schema at this revision even if the constant is
# later moved or renamed; ``tests/test_migration_org_id_constant.py`` pins every
# copy to ``app.org_context.APPREND_ORG_ID`` so "inline" cannot become "drifted".
#
# Getting this wrong is not a slow leak: the backfill below is followed by a
# foreign key to ``organizations``, so a literal naming no row fails the whole
# migration on any deployment that has ever created a group.
APPREND_ORG_ID = "a9910000-0000-4000-8000-000000000001"


def upgrade() -> None:
    op.add_column(
        "company_groups",
        sa.Column("org_id", UUID(as_uuid=True), nullable=True),
    )
    op.execute(f"UPDATE company_groups SET org_id = '{APPREND_ORG_ID}' WHERE org_id IS NULL")
    op.alter_column("company_groups", "org_id", nullable=False)
    op.execute(
        f"ALTER TABLE company_groups ALTER COLUMN org_id SET DEFAULT '{APPREND_ORG_ID}'"
    )
    op.create_index("ix_company_groups_org_id", "company_groups", ["org_id"])
    op.create_foreign_key(
        "fk_company_groups_org_id",
        "company_groups",
        "organizations",
        ["org_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # Memberships that cross the boundary the backfill just drew.
    op.execute(
        """
        DELETE FROM company_group_members m
        USING company_groups g, companies c
        WHERE m.group_id = g.id
          AND m.company_id = c.id
          AND c.org_id <> g.org_id
        """
    )


def downgrade() -> None:
    op.drop_constraint("fk_company_groups_org_id", "company_groups", type_="foreignkey")
    op.drop_index("ix_company_groups_org_id", table_name="company_groups")
    op.drop_column("company_groups", "org_id")
