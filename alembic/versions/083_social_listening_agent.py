"""Social Listening Agent — config, discovered signals, monitored companies.

Chains off ``082_referral_agent``, the current head.

Three org-scoped tables backing the buying-intent listening agent:

* ``social_listening_agents`` — one config row per org (ON/OFF toggle, LLM model
  + encrypted key, watched keywords/industries/sources, automation thresholds).
* ``signal_events`` — discovered public signals with AI relevance score + type.
* ``monitored_companies`` — companies the org has asked the agent to watch.

Every table carries ``org_id`` (FK → organizations, ``server_default`` = the
first-party Apprend org) matching the multi-tenancy convention from migration
078, so a raw insert without a bound org still lands on the right tenant and the
``OrgScopedMixin`` read filter isolates it.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "083_social_listening_agent"
down_revision = "082_referral_agent"
branch_labels = None
depends_on = None

# Must match app.org_context.APPREND_ORG_ID (as in migration 078).
APPREND_ORG_ID = "a9910000-0000-4000-8000-000000000001"

DEFAULT_LLM_MODEL = "meta-llama/llama-3.3-70b-instruct:free"


def _org_id_column() -> sa.Column:
    """The shared org_id column: FK → organizations, defaulting to Apprend."""
    return sa.Column(
        "org_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        server_default=APPREND_ORG_ID,
    )


def upgrade() -> None:
    # --- social_listening_agents ------------------------------------------
    op.create_table(
        "social_listening_agents",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        _org_id_column(),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("llm_model", sa.String(120), nullable=False, server_default=DEFAULT_LLM_MODEL),
        sa.Column("llm_api_key", sa.Text(), nullable=True),
        sa.Column("monitored_keywords", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("monitored_industries", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("news_sources", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("min_relevance_score", sa.Integer(), nullable=False, server_default="70"),
        sa.Column("auto_create_leads", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("auto_enroll_dialtone", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("scan_frequency_hours", sa.Integer(), nullable=False, server_default="24"),
        sa.Column("last_scan_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        # One config row per organization.
        sa.UniqueConstraint("org_id", name="uq_social_listening_agents_org"),
    )
    op.create_index(
        "ix_social_listening_agents_org_id", "social_listening_agents", ["org_id"]
    )

    # --- signal_events ----------------------------------------------------
    op.create_table(
        "signal_events",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        _org_id_column(),
        sa.Column("source_type", sa.String(30), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("company_name", sa.String(255), nullable=False),
        sa.Column("company_domain", sa.String(255), nullable=True),
        sa.Column(
            "company_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("headline", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("relevance_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("signal_type", sa.String(30), nullable=True),
        sa.Column("raw_data", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="new"),
        sa.Column("lead_created", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "contact_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_signal_events_org_id", "signal_events", ["org_id"])
    op.create_index("ix_signal_events_source_type", "signal_events", ["source_type"])
    op.create_index("ix_signal_events_company_name", "signal_events", ["company_name"])
    op.create_index("ix_signal_events_company_domain", "signal_events", ["company_domain"])
    op.create_index("ix_signal_events_company_id", "signal_events", ["company_id"])
    op.create_index("ix_signal_events_relevance_score", "signal_events", ["relevance_score"])
    op.create_index("ix_signal_events_signal_type", "signal_events", ["signal_type"])
    op.create_index("ix_signal_events_status", "signal_events", ["status"])
    op.create_index("ix_signal_events_created_at", "signal_events", ["created_at"])

    # --- monitored_companies ----------------------------------------------
    op.create_table(
        "monitored_companies",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        _org_id_column(),
        sa.Column("company_name", sa.String(255), nullable=False),
        sa.Column("domain", sa.String(255), nullable=True),
        sa.Column(
            "company_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("monitor_news", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("monitor_jobs", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_checked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_monitored_companies_org_id", "monitored_companies", ["org_id"])
    op.create_index("ix_monitored_companies_company_name", "monitored_companies", ["company_name"])
    op.create_index("ix_monitored_companies_company_id", "monitored_companies", ["company_id"])


def downgrade() -> None:
    op.drop_table("monitored_companies")
    op.drop_table("signal_events")
    op.drop_table("social_listening_agents")
