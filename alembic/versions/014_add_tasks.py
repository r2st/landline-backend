"""Add tasks table (actionable to-dos for client management)

Revision ID: 014_add_tasks
Revises: 013_add_deals
Create Date: 2026-06-22
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "014_add_tasks"
down_revision = "013_add_deals"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tasks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id",
            UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "contact_id",
            UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("due_date", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("priority", sa.String(20), nullable=False, server_default="medium"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("assigned_to", sa.String(255), nullable=True),
        sa.Column("task_type", sa.String(20), nullable=False, server_default="follow_up"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("priority IN ('low','medium','high','urgent')", name="ck_tasks_priority"),
        sa.CheckConstraint("status IN ('pending','in_progress','completed','cancelled')", name="ck_tasks_status"),
        sa.CheckConstraint("task_type IN ('follow_up','meeting','call','email','review')", name="ck_tasks_type"),
    )
    op.create_index("ix_tasks_company_id", "tasks", ["company_id"])
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_index("ix_tasks_due_date", "tasks", ["due_date"])


def downgrade():
    op.drop_index("ix_tasks_due_date", table_name="tasks")
    op.drop_index("ix_tasks_status", table_name="tasks")
    op.drop_index("ix_tasks_company_id", table_name="tasks")
    op.drop_table("tasks")
