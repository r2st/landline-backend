"""Add AI Workflow Builder tables (feature 4.3).

Creates ``workflows`` (event-triggered automations with conditions and action
steps) and ``workflow_runs`` (per-execution tracking).

Revision ID: 051_add_workflows
Revises: 050_add_integrations
Create Date: 2026-07-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "051_add_workflows"
down_revision = "050_add_integrations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflows",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("trigger_event", sa.String(80), nullable=False),
        sa.Column("trigger_conditions", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("steps", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("max_runs_per_day", sa.Integer, nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_workflows_org_id", "workflows", ["org_id"])
    op.create_index("ix_workflows_trigger_event", "workflows", ["trigger_event"])

    op.create_table(
        "workflow_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("workflow_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("trigger_payload", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("step_results", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index("ix_workflow_runs_workflow_id", "workflow_runs", ["workflow_id"])
    op.create_index("ix_workflow_runs_org_id", "workflow_runs", ["org_id"])
    op.create_index("ix_workflow_runs_status", "workflow_runs", ["status"])


def downgrade() -> None:
    op.drop_table("workflow_runs")
    op.drop_index("ix_workflows_trigger_event", table_name="workflows")
    op.drop_index("ix_workflows_org_id", table_name="workflows")
    op.drop_table("workflows")
