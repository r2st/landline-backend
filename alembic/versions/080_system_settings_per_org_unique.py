"""Make system_settings unique per organization, not globally.

``system_settings`` mixes in ``OrgScopedMixin`` (migration 078 added ``org_id``),
but its ``key`` column kept the *global* unique constraint from migration 058
(``uq_system_settings_key`` on ``key`` alone). That means only ONE organization
could ever hold a given setting — the moment a second tenant tried to set
``sending_enabled`` (its kill switch) or ``warmup_start_date`` the insert would
violate the unique constraint. It also meant an unscoped lookup could match rows
from several orgs.

Swap the global unique on ``key`` for a composite unique on
``(org_id, key)`` so every organization has its own independent settings row per
key. The plain (non-unique) ``ix_system_settings_key`` lookup index is left in
place.

Revision ID: 080_system_settings_per_org_unique
Revises: 079_platform_superadmin
"""

from alembic import op

revision = "080_system_settings_per_org_unique"
down_revision = "079_platform_superadmin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF EXISTS: some environments created the table without the named
    # constraint (schema drift), so an unconditional drop would fail there.
    op.execute("ALTER TABLE system_settings DROP CONSTRAINT IF EXISTS uq_system_settings_key")
    op.create_unique_constraint(
        "uq_system_settings_org_key", "system_settings", ["org_id", "key"]
    )


def downgrade() -> None:
    op.execute("ALTER TABLE system_settings DROP CONSTRAINT IF EXISTS uq_system_settings_org_key")
    # Best-effort restore of the global unique. This can fail if more than one
    # org now holds the same key — which is exactly what this migration enabled —
    # so it is intentionally left to the operator to reconcile first.
    op.create_unique_constraint(
        "uq_system_settings_key", "system_settings", ["key"]
    )
