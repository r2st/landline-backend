"""Extend interactions with content, follow_up_date, created_at

Adds the client-management fields to the existing interactions table:
- content: free-form body (meeting notes, call summary, email text)
- follow_up_date: optional reminder date
- created_at: row creation timestamp (back-fills existing rows via server default)

No check constraint is added on interaction_type: internal writers (reply
tracker, email logger) use values beyond the meeting/call/email/note set, which
the /interactions create endpoint validates at the API layer instead.

Revision ID: 015_extend_interactions
Revises: 014_add_tasks
Create Date: 2026-06-22
"""
from alembic import op
import sqlalchemy as sa

revision = "015_extend_interactions"
down_revision = "014_add_tasks"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("interactions", sa.Column("content", sa.Text, nullable=True))
    op.add_column("interactions", sa.Column("follow_up_date", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column(
        "interactions",
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_interactions_company_id", "interactions", ["company_id"])
    op.create_index("ix_interactions_occurred_at", "interactions", ["occurred_at"])


def downgrade():
    op.drop_index("ix_interactions_occurred_at", table_name="interactions")
    op.drop_index("ix_interactions_company_id", table_name="interactions")
    op.drop_column("interactions", "created_at")
    op.drop_column("interactions", "follow_up_date")
    op.drop_column("interactions", "content")
