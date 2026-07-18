"""Referral & warm-intro agent tables.

Chains off ``081_deal_progression_agent``, the current head.

Adds the three org-scoped tables behind the autonomous referral agent:

* ``referral_agents`` — one config row per organization (ON/OFF toggle, LLM
  model + encrypted key, candidate thresholds, message templates).
* ``referrals`` — the referral lifecycle (identified → converted / declined),
  linking a referrer contact to a referred party and, on conversion, to revenue.
* ``referral_actions`` — the agent's proposed asks / follow-ups / thank-yous,
  each carrying the AI-drafted message and its approval state.

Every table carries ``org_id`` (NOT NULL, default the first-party Apprend org)
so it participates in the multi-tenant read filter, matching every other
tenant-owned table.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "082_referral_agent"
down_revision = "081_deal_progression_agent"
branch_labels = None
depends_on = None

# The fixed first-party org id (see app.org_context.APPREND_ORG_ID). Used as the
# server_default so a raw INSERT that omits org_id still lands on a real org.
_APPREND_ORG_ID = "a9910000-0000-4000-8000-000000000001"


def upgrade() -> None:
    op.create_table(
        "referral_agents",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "org_id", postgresql.UUID(as_uuid=True), nullable=False,
            server_default=_APPREND_ORG_ID,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "llm_model", sa.String(120), nullable=False,
            server_default="meta-llama/llama-3.3-70b-instruct:free",
        ),
        sa.Column("llm_api_key", sa.Text(), nullable=True),
        sa.Column("min_health_score", sa.Integer(), nullable=False, server_default="80"),
        sa.Column("min_relationship_days", sa.Integer(), nullable=False, server_default="90"),
        sa.Column("max_asks_per_client", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("followup_after_days", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("auto_ask_enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("referral_template", sa.Text(), nullable=True),
        sa.Column("followup_template", sa.Text(), nullable=True),
        sa.Column("thankyou_template", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "min_health_score >= 0 AND min_health_score <= 100",
            name="ck_referral_agents_health_range",
        ),
    )
    op.create_index("ix_referral_agents_org_id", "referral_agents", ["org_id"])
    # One config row per organization.
    op.create_index(
        "uq_referral_agents_org", "referral_agents", ["org_id"], unique=True,
    )

    op.create_table(
        "referrals",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "org_id", postgresql.UUID(as_uuid=True), nullable=False,
            server_default=_APPREND_ORG_ID,
        ),
        sa.Column("referrer_contact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("referred_name", sa.String(200), nullable=True),
        sa.Column("referred_email", sa.String(255), nullable=True),
        sa.Column("referred_company", sa.String(255), nullable=True),
        sa.Column("referred_contact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="identified"),
        sa.Column("ask_sent_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("received_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("converted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("referral_value", sa.BigInteger(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["referrer_contact_id"], ["contacts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["referred_contact_id"], ["contacts.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "status IN ('identified','asked','pending','received','converted','declined')",
            name="ck_referrals_status",
        ),
    )
    op.create_index("ix_referrals_org_id", "referrals", ["org_id"])
    op.create_index("ix_referrals_referrer_contact_id", "referrals", ["referrer_contact_id"])
    op.create_index("ix_referrals_referred_contact_id", "referrals", ["referred_contact_id"])
    op.create_index("ix_referrals_referred_email", "referrals", ["referred_email"])
    op.create_index("ix_referrals_status", "referrals", ["status"])

    op.create_table(
        "referral_actions",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "org_id", postgresql.UUID(as_uuid=True), nullable=False,
            server_default=_APPREND_ORG_ID,
        ),
        sa.Column("referrer_contact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("referral_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action_type", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("ai_subject", sa.Text(), nullable=True),
        sa.Column("ai_message", sa.Text(), nullable=True),
        sa.Column("executed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["referrer_contact_id"], ["contacts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["referral_id"], ["referrals.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "action_type IN ('initial_ask','followup','thank_you')",
            name="ck_referral_actions_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending','sent','dismissed')",
            name="ck_referral_actions_status",
        ),
    )
    op.create_index("ix_referral_actions_org_id", "referral_actions", ["org_id"])
    op.create_index("ix_referral_actions_referrer_contact_id", "referral_actions", ["referrer_contact_id"])
    op.create_index("ix_referral_actions_referral_id", "referral_actions", ["referral_id"])
    op.create_index("ix_referral_actions_status", "referral_actions", ["status"])


def downgrade() -> None:
    op.drop_table("referral_actions")
    op.drop_index("ix_referrals_status", table_name="referrals")
    op.drop_index("ix_referrals_referred_email", table_name="referrals")
    op.drop_index("ix_referrals_referred_contact_id", table_name="referrals")
    op.drop_index("ix_referrals_referrer_contact_id", table_name="referrals")
    op.drop_index("ix_referrals_org_id", table_name="referrals")
    op.drop_table("referrals")
    op.drop_index("uq_referral_agents_org", table_name="referral_agents")
    op.drop_index("ix_referral_agents_org_id", table_name="referral_agents")
    op.drop_table("referral_agents")
