"""Promote the platform superadmin.

Chains off ``078_multi_tenancy``.

Upgrades ``sumaninster7@gmail.com`` from ``org_admin`` to ``platform_admin`` and
detaches them from any single organization (``org_id`` → NULL). Platform admins
manage every organization and are exempt from org-level data isolation, so they
must not be bound to one org (see ``app/models/user.py``).

Idempotent and safe to run on any environment: the UPDATE simply matches zero
rows if the account doesn't exist yet (e.g. a fresh DB), and re-running it is a
no-op because the row is already in the target state.
"""

from alembic import op
import sqlalchemy as sa

revision = "079_platform_superadmin"
down_revision = "078_multi_tenancy"
branch_labels = None
depends_on = None

SUPERADMIN_EMAIL = "sumaninster7@gmail.com"

# Fixed id of the first-party org — used only to restore membership on downgrade.
# MUST match app.org_context.APPREND_ORG_ID / migration 078.
APPREND_ORG_ID = "a9910000-0000-4000-8000-000000000001"


def upgrade() -> None:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            """
            UPDATE users
               SET role = 'platform_admin',
                   org_id = NULL
             WHERE email = :email
            """
        ).bindparams(email=SUPERADMIN_EMAIL)
    )
    # rowcount is informational; a fresh DB legitimately matches nothing.
    print(f"[079] Promoted {result.rowcount} user(s) ({SUPERADMIN_EMAIL}) to platform_admin")


def downgrade() -> None:
    # Revert to an org-scoped admin bound to the Apprend organization.
    op.execute(
        sa.text(
            """
            UPDATE users
               SET role = 'org_admin',
                   org_id = CAST(:org_id AS uuid)
             WHERE email = :email
               AND role = 'platform_admin'
            """
        ).bindparams(email=SUPERADMIN_EMAIL, org_id=APPREND_ORG_ID)
    )
