"""P5 — unified ingestion pipeline: dedup keys, credit ledger, country allowlist.

Chains off ``067_hunter_verification`` (P3), which landed on main while this
was in flight and had itself chained off ``066``. Chaining onto it keeps the
chain linear rather than opening a second head that would need a merge revision.
The spec's warning about three revisions branching off ``059`` is stale — that
branch was already reconciled by ``063_merge_dialtone_features``.

Four changes:

* ``companies.name_key`` — the canonical match key the one shared dedup rule
  reads, backfilled for every existing row. Deliberately **not** unique:
  "Precision Manufacturing" in Ohio and in Pune legitimately collide, which is
  why the rule pairs the key with geography before merging anything.
* ``prospect_candidates.dedup_confidence`` / ``merged_into_company_id`` — how
  sure the match was, and where a human-confirmed merge landed.
* ``api_credit_ledger`` — one row per paid external call. The budget gate sums
  it *before* spending, so the daily prospecting run can no longer burn a month
  of Apollo credits unmetered.
* ``ck_companies_country`` widened from ``('US','CA')`` to an allowlist. Every
  existing row is US/CA and satisfies the wider constraint, so this is safe in
  both directions for current data.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.constants import ALLOWED_COUNTRIES_SQL
from app.services.normalize import normalize_name_key

revision = "068_ingestion_dedup"
down_revision = "067_hunter_verification"
branch_labels = None
depends_on = None

# Rows per backfill batch. The key is computed in Python rather than SQL on
# purpose: it must be byte-identical to what normalize_name_key produces at
# runtime, and re-implementing accent folding plus legal-suffix stripping in SQL
# would drift the moment either side changed.
_BATCH = 1000


def upgrade() -> None:
    # --- companies.name_key -------------------------------------------------
    op.add_column("companies", sa.Column("name_key", sa.String(255), nullable=True))

    bind = op.get_bind()
    # Keyset pagination on the primary key. A "WHERE name_key IS NULL" cursor
    # would spin forever on any name that canonicalizes to nothing (e.g. "..."),
    # since such a row never stops matching the filter.
    last_id = None
    while True:
        if last_id is None:
            rows = bind.execute(
                sa.text(
                    "SELECT id, name FROM companies ORDER BY id LIMIT :limit"
                ),
                {"limit": _BATCH},
            ).fetchall()
        else:
            rows = bind.execute(
                sa.text(
                    "SELECT id, name FROM companies "
                    "WHERE id > :last ORDER BY id LIMIT :limit"
                ),
                {"last": last_id, "limit": _BATCH},
            ).fetchall()
        if not rows:
            break
        last_id = rows[-1][0]
        updates = []
        for pk, name in rows:
            key = normalize_name_key(name)
            if key:
                updates.append({"pk": pk, "key": key})
        if updates:
            bind.execute(
                sa.text("UPDATE companies SET name_key = :key WHERE id = :pk"), updates
            )

    op.create_index("ix_companies_name_key", "companies", ["name_key"])

    # --- prospect_candidates dedup provenance -------------------------------
    op.add_column(
        "prospect_candidates", sa.Column("dedup_confidence", sa.String(10), nullable=True)
    )
    op.add_column(
        "prospect_candidates",
        sa.Column("merged_into_company_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_prospect_candidates_merged_into_company",
        "prospect_candidates",
        "companies",
        ["merged_into_company_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # --- api_credit_ledger --------------------------------------------------
    op.create_table(
        "api_credit_ledger",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("operation", sa.String(30), nullable=False),
        sa.Column("credits", sa.Integer, nullable=False, server_default="1"),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("icp_profile_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(15), nullable=False, server_default="ok"),
        sa.Column("details", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["run_id"], ["prospecting_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["icp_profile_id"], ["icp_profiles.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_api_credit_ledger_run_id", "api_credit_ledger", ["run_id"])
    op.create_index(
        "ix_api_credit_ledger_icp_profile_id", "api_credit_ledger", ["icp_profile_id"]
    )
    op.create_index("ix_api_credit_ledger_created_at", "api_credit_ledger", ["created_at"])
    # The budget-window sum is always (provider, created_at >= cutoff).
    op.create_index(
        "ix_api_credit_ledger_provider_created",
        "api_credit_ledger",
        ["provider", "created_at"],
    )

    # --- country allowlist --------------------------------------------------
    # An allowlist rather than dropping validation: 'XX' still fails loudly.
    op.execute("ALTER TABLE companies DROP CONSTRAINT IF EXISTS ck_companies_country")
    op.create_check_constraint(
        "ck_companies_country", "companies", f"country IN ({ALLOWED_COUNTRIES_SQL})"
    )


def downgrade() -> None:
    # Narrowing the constraint would fail against any non-NA row this migration
    # made insertable, so relabel them first. Downgrade is lossy by nature here
    # — that is the honest trade for un-widening a market.
    op.execute("ALTER TABLE companies DROP CONSTRAINT IF EXISTS ck_companies_country")
    op.execute("UPDATE companies SET country = 'US' WHERE country NOT IN ('US', 'CA')")
    op.create_check_constraint(
        "ck_companies_country", "companies", "country IN ('US', 'CA')"
    )

    op.drop_index("ix_api_credit_ledger_provider_created", table_name="api_credit_ledger")
    op.drop_index("ix_api_credit_ledger_created_at", table_name="api_credit_ledger")
    op.drop_index("ix_api_credit_ledger_icp_profile_id", table_name="api_credit_ledger")
    op.drop_index("ix_api_credit_ledger_run_id", table_name="api_credit_ledger")
    op.drop_table("api_credit_ledger")

    # Dropping the column drops its FK with it, so there's no need to name the
    # constraint here — and naming it would break against any database where the
    # column was created by the models rather than by this migration.
    op.drop_column("prospect_candidates", "merged_into_company_id")
    op.drop_column("prospect_candidates", "dedup_confidence")

    op.drop_index("ix_companies_name_key", table_name="companies")
    op.drop_column("companies", "name_key")
