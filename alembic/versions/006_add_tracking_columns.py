"""Add email tracking columns

Revision ID: 006_add_tracking_columns
Revises: 005_add_indexes
Create Date: 2026-06-20
"""
from alembic import op
import sqlalchemy as sa

revision = '006_add_tracking_columns'
down_revision = '005_add_indexes'
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('email_sends', sa.Column('opened_at', sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column('email_sends', sa.Column('clicked_at', sa.TIMESTAMP(timezone=True), nullable=True))

def downgrade():
    op.drop_column('email_sends', 'clicked_at')
    op.drop_column('email_sends', 'opened_at')
