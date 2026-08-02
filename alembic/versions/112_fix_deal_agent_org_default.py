"""Correct the org_id default on the two tables migration 081 created.

081 inlined ``00000000-0000-0000-0000-000000000001`` as the ``server_default``
for ``deal_progression_agents.org_id`` and ``deal_actions.org_id``. The Apprend
organization row that migration 078 inserts is
``a9910000-0000-4000-8000-000000000001``; the id 081 used matches no row in
``organizations``, and both columns carry a foreign key to it.

Nothing failed, which is why it survived: the tables were new and empty, and
every insert the application makes is an ORM insert, which the ``before_flush``
hook in :mod:`app.models.mixins` stamps with the caller's org before the default
is ever consulted. The default is what a *non*-ORM write falls back on — a
backfill script, a ``COPY``, a psql session, a future migration — and there it
is not a wrong-tenant row but an immediate foreign-key violation.

081's own literal is corrected too, so a database built from scratch never
carries this; this migration is for the ones that already do. Both are needed:
editing 081 alone fixes only future builds, and adding 112 alone leaves the
fresh-build path wrong.

Idempotent and safe to run against a database that never had the bad value:
it sets the default to the correct id unconditionally.

Revision ID: 112_fix_deal_agent_org_default
Revises: 111_company_groups_org_id
"""

from alembic import op

revision = "112_fix_deal_agent_org_default"
down_revision = "111_company_groups_org_id"
branch_labels = None
depends_on = None

APPREND_ORG_ID = "a9910000-0000-4000-8000-000000000001"
BAD_ORG_ID = "00000000-0000-0000-0000-000000000001"

TABLES = ("deal_progression_agents", "deal_actions")


def upgrade() -> None:
    for table in TABLES:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN org_id SET DEFAULT '{APPREND_ORG_ID}'"
        )
        # Belt and braces. No row can hold the bad id today — the FK forbids it —
        # but if the constraint was ever dropped and rebuilt NOT VALID, this is
        # the one moment we are looking at these columns.
        op.execute(
            f"UPDATE {table} SET org_id = '{APPREND_ORG_ID}' "
            f"WHERE org_id = '{BAD_ORG_ID}'"
        )


def downgrade() -> None:
    """Deliberately not restoring the broken default.

    A downgrade exists to undo a schema *change*; the change here was from an id
    that names no organization to the one that does. Re-installing a default
    which fails its own foreign key is not a state worth being able to return to.
    """
    pass
