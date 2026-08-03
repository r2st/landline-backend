"""Scope the template/signature uniques to an org, so a second tenant can have one.

``email_templates`` (migration 002) and ``email_signatures`` (migration 019)
each carry two constraints written before this product had tenants:

    UNIQUE (name)
    UNIQUE (is_default) WHERE is_default        -- a partial unique index

Migration 078 added ``org_id`` to both tables and widened neither. That is not
an oversight anyone could have caught by reading the diff: scoping is declared
by mixing ``OrgScopedMixin`` into a model, autogenerate compares the models
against the schema, and both models declared their constraints exactly as the
database already held them. There was nothing to report.

What the default index actually says
------------------------------------
``UNIQUE (is_default) WHERE is_default`` permits **one row with is_default =
true in the whole table** — across every organization on the deployment. Both
002 and 019 seed that row into what became the first-party org.

So the second organization to exist cannot have a default template or a default
signature. And the way it fails is worse than a plain error:

* ``create_email_signature`` marks a tenant's *first* signature default
  automatically. Its "is there already a default?" read is org-scoped and
  correctly answers no. The insert then collides with a row belonging to a
  tenant it cannot see.
* Both handlers wrap that insert in ``except Exception`` and answer **409 "a
  signature/template with that name already exists"**. The name is not the
  problem, the tenant has no such name, and no amount of renaming helps.
* A tenant who works around it by leaving ``is_default`` off gets the quiet
  failure instead: ``email_dispatch.send_outreach_email`` appends the
  ``is_default`` signature to every outbound message, finds none for that org,
  and sends unsigned mail with nothing logged.

``UNIQUE (name)`` is the same shape one step milder — one tenant naming a
signature "Default" stops every other tenant from doing so, and the 409
confirms the name exists somewhere, which is a read across the boundary.

Widening is safe in the direction it runs
-----------------------------------------
Every row that satisfied ``UNIQUE (name)`` satisfies ``UNIQUE (org_id, name)``,
and likewise for the partial index, so the upgrade cannot fail on existing
data — there is nothing to backfill or reconcile. All the historical rows are in
one org anyway, which is precisely why nobody hit this on this deployment: it
is a bug you only meet on the *second* tenant.

The downgrade can fail, and legitimately: once two orgs each hold a default, no
platform-wide unique index describes the table. Rather than silently choosing a
survivor — which would clear a tenant's default sign-off to satisfy a schema
they did not ask for — it demotes the duplicates to non-default, keeping the
oldest, and logs nothing it cannot honestly claim. Name collisions across orgs
are left to fail loudly, because renaming another tenant's row is not a
migration's call to make.

Revision ID: 114_org_scoped_template_signature_uniques
Revises: 113_bounce_kind_blocked
"""

import sqlalchemy as sa
from alembic import op

revision = "114_org_scoped_template_signature_uniques"
down_revision = "113_bounce_kind_blocked"
branch_labels = None
depends_on = None


# (table, old name constraint, new name constraint, old default index, new one)
_TABLES = (
    (
        "email_templates",
        "uq_email_templates_name",
        "uq_email_templates_org_name",
        "uq_email_templates_one_default",
        "uq_email_templates_org_one_default",
    ),
    (
        "email_signatures",
        "uq_email_signatures_name",
        "uq_email_signatures_org_name",
        "uq_email_signatures_one_default",
        "uq_email_signatures_org_one_default",
    ),
)


def upgrade() -> None:
    for table, old_name, new_name, old_default, new_default in _TABLES:
        # Drop first: the old name constraint is a strict subset of the new one,
        # so holding both would keep the platform-wide rule in force and the
        # migration would appear to do nothing.
        op.drop_constraint(old_name, table, type_="unique")
        op.create_unique_constraint(new_name, table, ["org_id", "name"])

        op.drop_index(old_default, table_name=table)
        op.create_index(
            new_default,
            table,
            ["org_id", "is_default"],
            unique=True,
            postgresql_where=sa.text("is_default"),
        )


def downgrade() -> None:
    for table, old_name, new_name, old_default, new_default in _TABLES:
        op.drop_index(new_default, table_name=table)
        # Rows the old index cannot describe: every default past the first.
        # Demote them rather than delete them — the signature/template is still
        # the tenant's, it just stops being their default, which is the only
        # part the narrower schema has room for. Oldest wins so the choice is
        # not arbitrary.
        op.execute(
            f"""
            UPDATE {table} SET is_default = false
            WHERE is_default
              AND id <> (
                SELECT id FROM {table}
                WHERE is_default
                ORDER BY created_at ASC, id ASC
                LIMIT 1
              )
            """
        )
        op.create_index(
            old_default,
            table,
            ["is_default"],
            unique=True,
            postgresql_where=sa.text("is_default"),
        )

        # Left to fail if two orgs share a name. Renaming another tenant's row
        # to satisfy a schema rollback is not a decision a migration should take
        # on its own.
        op.drop_constraint(new_name, table, type_="unique")
        op.create_unique_constraint(old_name, table, ["name"])
