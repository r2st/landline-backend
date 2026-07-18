"""Client Success Agent — config + action tables.

Chains off ``085_meeting_booking_agent``, the current head.

Adds the two org-scoped tables behind the autonomous Client Success Agent:

* ``client_success_agent`` — one config row per organization (master on/off
  switch, LLM model + encrypted key, health thresholds, scan cadence, per-play
  auto toggles, editable templates). ``org_id`` is UNIQUE so there is exactly
  one config per tenant.
* ``client_success_action`` — the queue of actions the agent decides to take
  (check-in / escalation / celebration / review_call), each with its trigger
  reason, the health score at trigger time, lifecycle status, and the
  AI-generated message.

Both mix in ``OrgScopedMixin`` so they carry a NOT NULL ``org_id`` FK to
``organizations`` (server_default = the first-party Apprend org, matching the
mixin) and are covered by the global tenant read filter.

Revision ID: 086_client_success_agent
Revises: 085_meeting_booking_agent
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "086_client_success_agent"
down_revision = "085_meeting_booking_agent"
branch_labels = None
depends_on = None

_APPREND_ORG_ID = "a9910000-0000-4000-8000-000000000001"
_DEFAULT_MODEL = "meta-llama/llama-3.3-70b-instruct:free"


def upgrade() -> None:
    op.create_table(
        "client_success_agent",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "org_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False, server_default=_APPREND_ORG_ID,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("llm_model", sa.String(120), nullable=False, server_default=_DEFAULT_MODEL),
        sa.Column("llm_api_key", sa.Text(), nullable=True),
        sa.Column("health_warning_threshold", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("health_critical_threshold", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("check_frequency_days", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("auto_checkin_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("auto_escalation_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("celebration_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("checkin_template", sa.Text(), nullable=True),
        sa.Column("escalation_template", sa.Text(), nullable=True),
        sa.Column("celebration_template", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("org_id", name="uq_client_success_agent_org"),
    )
    op.create_index(
        "ix_client_success_agent_org_id", "client_success_agent", ["org_id"],
    )

    op.create_table(
        "client_success_action",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "org_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False, server_default=_APPREND_ORG_ID,
        ),
        sa.Column(
            "contact_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column(
            "company_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=True,
        ),
        sa.Column("action_type", sa.String(20), nullable=False),
        sa.Column("trigger_reason", sa.Text(), nullable=True),
        sa.Column("health_score_at_trigger", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("ai_message", sa.Text(), nullable=True),
        sa.Column("executed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "action_type IN ('checkin','escalation','celebration','review_call')",
            name="ck_client_success_action_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending','sent','dismissed','completed')",
            name="ck_client_success_action_status",
        ),
    )
    op.create_index("ix_client_success_action_org_id", "client_success_action", ["org_id"])
    op.create_index("ix_client_success_action_contact_id", "client_success_action", ["contact_id"])
    op.create_index("ix_client_success_action_company_id", "client_success_action", ["company_id"])
    op.create_index("ix_client_success_action_action_type", "client_success_action", ["action_type"])
    op.create_index("ix_client_success_action_status", "client_success_action", ["status"])
    op.create_index("ix_client_success_action_created_at", "client_success_action", ["created_at"])


def downgrade() -> None:
    op.drop_table("client_success_action")
    op.drop_index("ix_client_success_agent_org_id", table_name="client_success_agent")
    op.drop_table("client_success_agent")
