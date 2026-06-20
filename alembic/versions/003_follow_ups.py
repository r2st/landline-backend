"""Follow-up reminders for outreach follow-up tracking.

Adds the follow_ups table: lightweight reminders attached to a contact
and/or company with a due date and status, powering the follow-up tracking
view (overdue, upcoming, and completed reminders).

Revision ID: 003_follow_ups
Revises: 002_email_templates
Create Date: 2026-06-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "003_follow_ups"
down_revision: Union[str, None] = "002_email_templates"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "follow_ups",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("due_date", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "status IN ('pending','completed','dismissed')",
            name="ck_follow_up_status",
        ),
    )
    op.create_index("ix_follow_ups_status", "follow_ups", ["status"])
    op.create_index("ix_follow_ups_due_date", "follow_ups", ["due_date"])
    op.create_index("ix_follow_ups_contact", "follow_ups", ["contact_id"])
    op.create_index("ix_follow_ups_company", "follow_ups", ["company_id"])


def downgrade() -> None:
    op.drop_index("ix_follow_ups_company", table_name="follow_ups")
    op.drop_index("ix_follow_ups_contact", table_name="follow_ups")
    op.drop_index("ix_follow_ups_due_date", table_name="follow_ups")
    op.drop_index("ix_follow_ups_status", table_name="follow_ups")
    op.drop_table("follow_ups")
