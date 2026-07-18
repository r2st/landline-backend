"""Deal Progression Agent — per-org config + action log.

Chains off ``080_system_settings_per_org_unique``, the current head.

Two tenant-owned tables:

* ``deal_progression_agents`` — one config row per org (UNIQUE ``org_id``): the
  staleness thresholds, auto-advance / auto-follow-up switches, and escalation
  window the agent runs within.
* ``deal_actions`` — the append-only queue + audit trail of what the agent
  proposed or did on each deal.

Both carry ``org_id`` (UUID FK → organizations, ``server_default`` = Apprend, to
match the multi-tenant mixin) so the global read filter isolates them per tenant.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "081_deal_progression_agent"
down_revision = "080_system_settings_per_org_unique"
branch_labels = None
depends_on = None

# The first-party Apprend org id — the server-side default for org_id on raw
# inserts, matching OrgScopedMixin (see migration 078).
APPREND_ORG_ID = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    op.create_table(
        "deal_progression_agents",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "org_id", postgresql.UUID(as_uuid=True), nullable=False,
            server_default=APPREND_ORG_ID,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "stale_thresholds", postgresql.JSONB(), nullable=False,
            server_default="{}",
        ),
        sa.Column("auto_advance_enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("auto_followup_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("max_auto_followups_per_deal", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("escalation_threshold_days", sa.Integer(), nullable=False, server_default="14"),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["organizations.id"], ondelete="CASCADE",
        ),
        sa.UniqueConstraint("org_id", name="uq_deal_progression_agent_org"),
    )
    op.create_index(
        "ix_deal_progression_agents_org_id", "deal_progression_agents", ["org_id"],
    )

    op.create_table(
        "deal_actions",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "org_id", postgresql.UUID(as_uuid=True), nullable=False,
            server_default=APPREND_ORG_ID,
        ),
        sa.Column("deal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_type", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("ai_reasoning", sa.Text(), nullable=True),
        sa.Column("email_draft_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("email_subject", sa.Text(), nullable=True),
        sa.Column("email_body", sa.Text(), nullable=True),
        sa.Column("suggested_action", sa.Text(), nullable=True),
        sa.Column("executed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["deal_id"], ["deals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["email_draft_id"], ["ai_email_drafts.id"], ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "action_type IN ('auto_followup','suggestion','stage_change','escalation','at_risk_flag')",
            name="ck_deal_actions_action_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending','approved','executed','dismissed','expired')",
            name="ck_deal_actions_status",
        ),
    )
    op.create_index("ix_deal_actions_org_id", "deal_actions", ["org_id"])
    op.create_index("ix_deal_actions_deal_id", "deal_actions", ["deal_id"])
    op.create_index("ix_deal_actions_action_type", "deal_actions", ["action_type"])
    op.create_index("ix_deal_actions_status", "deal_actions", ["status"])
    op.create_index("ix_deal_actions_created_at", "deal_actions", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_deal_actions_created_at", table_name="deal_actions")
    op.drop_index("ix_deal_actions_status", table_name="deal_actions")
    op.drop_index("ix_deal_actions_action_type", table_name="deal_actions")
    op.drop_index("ix_deal_actions_deal_id", table_name="deal_actions")
    op.drop_index("ix_deal_actions_org_id", table_name="deal_actions")
    op.drop_table("deal_actions")

    op.drop_index("ix_deal_progression_agents_org_id", table_name="deal_progression_agents")
    op.drop_table("deal_progression_agents")
