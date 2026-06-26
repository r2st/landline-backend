"""Add threading fields to outreach_enrollments for Gmail thread chaining.

Revision ID: 024_enrollment_threading
Revises: 023_email_communication
Create Date: 2026-06-26
"""
import sqlalchemy as sa
from alembic import op

revision = "024_enrollment_threading"
down_revision = "023_email_communication"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "outreach_enrollments",
        sa.Column("gmail_thread_id", sa.String(255), nullable=True),
    )
    op.add_column(
        "outreach_enrollments",
        sa.Column("gmail_message_id", sa.String(255), nullable=True),
    )
    op.add_column(
        "outreach_enrollments",
        sa.Column(
            "sender_account_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("gmail_accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "outreach_enrollments",
        sa.Column(
            "next_send_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )


def downgrade():
    op.drop_column("outreach_enrollments", "next_send_at")
    op.drop_column("outreach_enrollments", "sender_account_id")
    op.drop_column("outreach_enrollments", "gmail_message_id")
    op.drop_column("outreach_enrollments", "gmail_thread_id")
