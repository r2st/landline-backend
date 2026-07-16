"""P4 — Google Places SMB discovery: saved searches + staged businesses.

Chains off ``068_ingestion_dedup`` (P5), the current head. P5 already did the
two things this capability was blocked on, so neither is repeated here: the
``companies.country`` CHECK is already an allowlist including ``IN``, and the
ingestion service already rejects an unsupported country instead of silently
relabelling it ``US``.

Two new tables, no alterations to existing ones:

* ``discovery_searches`` — a saved category x location query. Places has no
  "every manufacturer in India" call; coverage is many narrow searches, each
  capped at 60 results and each billed, so the unit of scheduling has to be
  small enough to switch off individually.
* ``discovered_businesses`` — Places results staged *before* they become
  companies. Places returns no emails and no people, so most staged rows never
  earn a company; keeping them here means the ones that don't cost a row rather
  than polluting the pipeline. ``place_id`` is unique — it's the strongest
  identity Places offers, the only field Google's terms let us cache
  indefinitely, and what stops a weekly re-run re-staging the same business.

Company-level dedup is deliberately **not** given a ``companies.google_place_id``
column: the one shared rule in ``app/services/ingestion.py`` already resolves a
Places business by domain, then name+geography, and ``discovered_businesses.
company_id`` records where each one landed.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "069_places_discovery"
down_revision = "068_ingestion_dedup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "discovery_searches",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("query", sa.String(500), nullable=False),
        sa.Column("location_text", sa.String(255), nullable=True),
        sa.Column("radius_km", sa.Integer(), nullable=True),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lng", sa.Float(), nullable=True),
        sa.Column("business_type", sa.String(100), nullable=True),
        sa.Column("country", sa.String(2), nullable=True),
        sa.Column(
            "schedule", sa.String(10), nullable=False, server_default="one_time"
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_run_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("results_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_status", sa.String(20), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("credits_spent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "meta", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "schedule IN ('one_time','daily','weekly')",
            name="ck_discovery_searches_schedule",
        ),
    )
    op.create_index(
        "ix_discovery_searches_user_id", "discovery_searches", ["user_id"]
    )
    op.create_index(
        "ix_discovery_searches_last_run_at", "discovery_searches", ["last_run_at"]
    )
    # The beat sweep always selects on (is_active, schedule).
    op.create_index(
        "ix_discovery_searches_active_schedule",
        "discovery_searches",
        ["is_active", "schedule"],
    )

    op.create_table(
        "discovered_businesses",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        # Uniqueness is declared on the index below rather than as a column
        # constraint: the model's `unique=True, index=True` compiles to a single
        # UNIQUE INDEX, and adding a constraint here too would leave Postgres
        # maintaining two indexes on the same column for one guarantee.
        sa.Column("place_id", sa.String(255), nullable=False),
        sa.Column("business_name", sa.String(255), nullable=False),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("city", sa.String(100), nullable=True),
        sa.Column("state", sa.String(100), nullable=True),
        # Intentionally *not* constrained to the companies allowlist: staging a
        # business we can't yet import is informative. The allowlist is enforced
        # at import by the ingestion service, which rejects, never relabels.
        sa.Column("country", sa.String(2), nullable=True),
        sa.Column("phone", sa.String(30), nullable=True),
        sa.Column("website", sa.Text(), nullable=True),
        sa.Column("domain", sa.String(255), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("email_source", sa.String(10), nullable=True),
        sa.Column("email_confidence", sa.Integer(), nullable=True),
        sa.Column("category", sa.String(100), nullable=True),
        sa.Column("rating", sa.Float(), nullable=True),
        sa.Column("review_count", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(10), nullable=False, server_default="new"),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column(
            "discovered_via",
            sa.String(30),
            nullable=False,
            server_default="google_places",
        ),
        sa.Column("search_query", sa.String(500), nullable=True),
        sa.Column(
            "search_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("discovery_searches.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "raw", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "discovered_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("imported_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('new','enriched','imported','skipped')",
            name="ck_discovered_businesses_status",
        ),
    )
    # The dedup anchor for repeat runs of the same search — and the only Places
    # field Google's terms permit caching indefinitely.
    op.create_index(
        "ix_discovered_businesses_place_id",
        "discovered_businesses",
        ["place_id"],
        unique=True,
    )
    op.create_index(
        "ix_discovered_businesses_business_name",
        "discovered_businesses",
        ["business_name"],
    )
    op.create_index(
        "ix_discovered_businesses_country", "discovered_businesses", ["country"]
    )
    op.create_index(
        "ix_discovered_businesses_domain", "discovered_businesses", ["domain"]
    )
    op.create_index(
        "ix_discovered_businesses_category", "discovered_businesses", ["category"]
    )
    op.create_index(
        "ix_discovered_businesses_status", "discovered_businesses", ["status"]
    )
    op.create_index(
        "ix_discovered_businesses_search_id", "discovered_businesses", ["search_id"]
    )
    op.create_index(
        "ix_discovered_businesses_company_id", "discovered_businesses", ["company_id"]
    )
    op.create_index(
        "ix_discovered_businesses_contact_id", "discovered_businesses", ["contact_id"]
    )
    op.create_index(
        "ix_discovered_businesses_created_at", "discovered_businesses", ["created_at"]
    )
    # The results list is always (status, newest first).
    op.create_index(
        "ix_discovered_businesses_status_created",
        "discovered_businesses",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("discovered_businesses")
    op.drop_table("discovery_searches")
