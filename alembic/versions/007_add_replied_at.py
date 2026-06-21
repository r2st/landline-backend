"""Add replied_at column to email_sends

Revision ID: 007_add_replied_at
Revises: 006_add_tracking_columns
Create Date: 2026-06-20
"""
from alembic import op
import sqlalchemy as sa

revision = '007_add_replied_at'
down_revision = '006_add_tracking_columns'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'email_sends',
        sa.Column('replied_at', sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_column('email_sends', 'replied_at')
