"""Add company groups

Revision ID: 008_add_company_groups
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = '008_add_company_groups'
down_revision = '007_add_replied_at'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        'company_groups',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('description', sa.Text, nullable=True),
        sa.Column('color', sa.String(20), nullable=True, server_default='#3b82f6'),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        'company_group_members',
        sa.Column('group_id', UUID(as_uuid=True), sa.ForeignKey('company_groups.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('company_id', UUID(as_uuid=True), sa.ForeignKey('companies.id', ondelete='CASCADE'), primary_key=True),
    )

def downgrade():
    op.drop_table('company_group_members')
    op.drop_table('company_groups')
