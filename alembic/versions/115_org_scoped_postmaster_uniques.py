"""Scope the Postmaster uniques to an org, so a second tenant can pull metrics.

Migration 093 created both Postmaster tables with platform-wide uniqueness:

    postmaster_accounts   UNIQUE (email)
                          UNIQUE (google_sub)
    postmaster_metrics    UNIQUE (domain, metric_date)

093 landed *after* 078 gave every table an ``org_id``, so this isn't a
pre-tenancy leftover — the tables were born multi-tenant and constrained as if
they weren't. And it is invisible on a single-tenant deployment, which is why it
survived: every one of these constraints holds perfectly until the day a second
organization exists.

What breaks, in order of how badly
----------------------------------
**A tenant's Postmaster pull dies on another tenant's row.** Google Postmaster
Tools is the *only* window this product has into Gmail's spam signal — Gmail
emits no FBL, so ``spam_rate`` here is the sole measure of how often recipients
mark our mail as spam. ``_upsert_metric`` reads through the org filter, so org B
looks for ``(acme.com, 2026-08-01)``, correctly does not find org A's row, and
inserts. The insert hits ``uq_postmaster_domain_date`` and the whole day's
transaction rolls back on ``db.commit()``. Org B never gets a metric row — not
for that domain, not for any domain pulled in the same task — and what they see
in the UI is an empty reputation dashboard, which reads as "no problems".

Two tenants sharing a domain is not exotic: an agency running Dialtone for
several client tenants off one sending domain hits it on day one, as does any
deployment where a domain is monitored by more than one org.

**A Google identity can be connected by exactly one tenant, ever.** With
``UNIQUE (google_sub)`` platform-wide, the OAuth callback's lookup had to be
global to be able to find its own row — and a global lookup finds *other
tenants'* rows too. So when org B connected a Google account org A already had,
the callback took the update branch on org A's row: it overwrote org A's
refresh/access tokens with org B's, reassigned ``user_id`` to org B's user, and
left ``org_id`` pointing at org A. Org B was told "Connected"; their org-scoped
status page showed nothing connected. Org A's daily pull carried on, now
authenticating as a user in a different company.

That is a cross-tenant write, and the credentials are the payload.

Widening is safe in the direction it runs
-----------------------------------------
Every row satisfying ``UNIQUE (domain, metric_date)`` satisfies
``UNIQUE (org_id, domain, metric_date)``, and likewise for the account
constraints, so the upgrade cannot fail on existing data. There is nothing to
backfill: the rows that exist are all in one org, which is exactly why nobody
met this.

The downgrade can legitimately fail. Once two orgs each hold a row for the same
domain and date — or each connected the same Google account — no platform-wide
unique index describes the table. Rather than pick a survivor, it deletes only
what it can justify deleting (duplicate *metric* rows, keeping the oldest, since
a metric row is a re-pullable cache of Google's numbers) and leaves account
collisions to fail loudly. Deleting one tenant's OAuth connection to satisfy a
schema rollback is not a migration's call to make.

Revision ID: 115_org_scoped_postmaster_uniques
Revises: 114_org_scoped_template_signature_uniques
"""

from alembic import op

revision = "115_org_scoped_postmaster_uniques"
down_revision = "114_org_scoped_template_signature_uniques"
branch_labels = None
depends_on = None


# (table, old constraint, new constraint, columns)
_UNIQUES = (
    (
        "postmaster_accounts",
        "uq_postmaster_accounts_email",
        "uq_postmaster_accounts_org_email",
        ["org_id", "email"],
        ["email"],
    ),
    (
        "postmaster_accounts",
        "uq_postmaster_accounts_google_sub",
        "uq_postmaster_accounts_org_google_sub",
        ["org_id", "google_sub"],
        ["google_sub"],
    ),
    (
        "postmaster_metrics",
        "uq_postmaster_domain_date",
        "uq_postmaster_org_domain_date",
        ["org_id", "domain", "metric_date"],
        ["domain", "metric_date"],
    ),
)


def upgrade() -> None:
    for table, old_name, new_name, new_cols, _old_cols in _UNIQUES:
        # Drop first: the old constraint is a strict subset of the new one, so
        # holding both would keep the platform-wide rule in force and the
        # migration would appear to have done nothing.
        op.drop_constraint(old_name, table, type_="unique")
        op.create_unique_constraint(new_name, table, new_cols)

    # The dropped uniques were also the only index on these lookup columns, and
    # both are read by equality on every OAuth callback.
    op.create_index(
        "ix_postmaster_accounts_email", "postmaster_accounts", ["email"]
    )
    op.create_index(
        "ix_postmaster_accounts_google_sub", "postmaster_accounts", ["google_sub"]
    )


def downgrade() -> None:
    op.drop_index("ix_postmaster_accounts_google_sub", table_name="postmaster_accounts")
    op.drop_index("ix_postmaster_accounts_email", table_name="postmaster_accounts")

    # Metric rows are a re-pullable cache of Google's own numbers, so collapsing
    # duplicates costs a day of history that the next pull restores. Oldest
    # wins, so the choice isn't arbitrary.
    op.execute(
        """
        DELETE FROM postmaster_metrics pm
        WHERE EXISTS (
            SELECT 1 FROM postmaster_metrics keep
            WHERE keep.domain = pm.domain
              AND keep.metric_date = pm.metric_date
              AND (keep.created_at, keep.id) < (pm.created_at, pm.id)
        )
        """
    )

    for table, old_name, new_name, _new_cols, old_cols in _UNIQUES:
        # Account collisions are left to fail. Two tenants' OAuth connections to
        # the same Google account are both real; discarding one to satisfy a
        # narrower schema is a decision for a human, not for a downgrade.
        op.drop_constraint(new_name, table, type_="unique")
        op.create_unique_constraint(old_name, table, old_cols)
