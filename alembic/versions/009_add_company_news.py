"""Add company news table

Revision ID: 009_add_company_news
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = '009_add_company_news'
down_revision = '008_add_company_groups'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        'company_news',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('company_id', UUID(as_uuid=True), sa.ForeignKey('companies.id', ondelete='CASCADE'), nullable=False),
        sa.Column('title', sa.String(500), nullable=False),
        sa.Column('summary', sa.Text, nullable=True),
        sa.Column('url', sa.String(1000), nullable=True),
        sa.Column('source', sa.String(200), nullable=True),
        sa.Column('published_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('discovered_at', sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column('category', sa.String(50), nullable=True),
        sa.Column('relevance_score', sa.Integer, nullable=True),
        sa.Column('ai_analysis', JSONB, nullable=True),
        sa.Column('is_actionable', sa.Boolean, server_default='false'),
        sa.Column('actioned', sa.Boolean, server_default='false'),
    )
    op.create_index('ix_company_news_company_id', 'company_news', ['company_id'])

def downgrade():
    op.drop_index('ix_company_news_company_id')
    op.drop_table('company_news')
