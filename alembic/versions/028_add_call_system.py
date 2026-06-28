"""Add cold-call system: call_tasks, call_scripts, call_notifications (+ seed scripts)

Revision ID: 028_add_call_system
Revises: 027_knowledge_base
Create Date: 2026-06-28
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

from app.services.call_scripts_data import seed_call_scripts

revision = "028_add_call_system"
down_revision = "027_knowledge_base"
branch_labels = None
depends_on = None


def upgrade():
    # --- call_tasks ---
    op.create_table(
        "call_tasks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "contact_id",
            UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "company_id",
            UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("assigned_to", sa.String(255), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("priority", sa.String(10), nullable=False, server_default="warm"),
        sa.Column("scheduled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(30), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("call_duration_seconds", sa.Integer, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('pending','completed','skipped','rescheduled')",
            name="ck_call_tasks_status",
        ),
        sa.CheckConstraint(
            "priority IN ('hot','warm','cold')",
            name="ck_call_tasks_priority",
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN "
            "('connected','voicemail','no_answer','gatekeeper','callback_scheduled')",
            name="ck_call_tasks_outcome",
        ),
    )
    op.create_index("ix_call_tasks_company_id", "call_tasks", ["company_id"])
    op.create_index("ix_call_tasks_contact_id", "call_tasks", ["contact_id"])
    op.create_index("ix_call_tasks_assigned_to", "call_tasks", ["assigned_to"])
    op.create_index("ix_call_tasks_status", "call_tasks", ["status"])
    op.create_index("ix_call_tasks_priority", "call_tasks", ["priority"])
    op.create_index("ix_call_tasks_scheduled_at", "call_tasks", ["scheduled_at"])

    # --- call_scripts ---
    op.create_table(
        "call_scripts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("industry", sa.String(40), nullable=False),
        sa.Column("script_type", sa.String(30), nullable=False, server_default="initial_cold_call"),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("opening_hook", sa.Text, nullable=False),
        sa.Column("qualifying_questions", JSONB, nullable=True),
        sa.Column("pain_points", JSONB, nullable=True),
        sa.Column("value_proposition", sa.Text, nullable=True),
        sa.Column("objection_handling", JSONB, nullable=True),
        sa.Column("call_to_action", sa.Text, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("industry", "script_type", name="uq_call_scripts_industry_type"),
        sa.CheckConstraint(
            "industry IN ('injection_molding','cnc_machining','metal_stamping','food_beverage')",
            name="ck_call_scripts_industry",
        ),
        sa.CheckConstraint(
            "script_type IN ('initial_cold_call','follow_up','voicemail')",
            name="ck_call_scripts_type",
        ),
    )
    op.create_index("ix_call_scripts_industry", "call_scripts", ["industry"])

    # --- call_notifications ---
    op.create_table(
        "call_notifications",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "call_task_id",
            UUID(as_uuid=True),
            sa.ForeignKey("call_tasks.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("notification_type", sa.String(30), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("sent_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("read_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "notification_type IN "
            "('upcoming_call','missed_call','follow_up_due','hot_lead_alert')",
            name="ck_call_notifications_type",
        ),
    )
    op.create_index("ix_call_notifications_call_task_id", "call_notifications", ["call_task_id"])
    op.create_index("ix_call_notifications_type", "call_notifications", ["notification_type"])
    op.create_index("ix_call_notifications_read_at", "call_notifications", ["read_at"])

    # --- seed industry call scripts ---
    seed_call_scripts(op.get_bind())


def downgrade():
    op.drop_index("ix_call_notifications_read_at", table_name="call_notifications")
    op.drop_index("ix_call_notifications_type", table_name="call_notifications")
    op.drop_index("ix_call_notifications_call_task_id", table_name="call_notifications")
    op.drop_table("call_notifications")

    op.drop_index("ix_call_scripts_industry", table_name="call_scripts")
    op.drop_table("call_scripts")

    op.drop_index("ix_call_tasks_scheduled_at", table_name="call_tasks")
    op.drop_index("ix_call_tasks_priority", table_name="call_tasks")
    op.drop_index("ix_call_tasks_status", table_name="call_tasks")
    op.drop_index("ix_call_tasks_assigned_to", table_name="call_tasks")
    op.drop_index("ix_call_tasks_contact_id", table_name="call_tasks")
    op.drop_index("ix_call_tasks_company_id", table_name="call_tasks")
    op.drop_table("call_tasks")
