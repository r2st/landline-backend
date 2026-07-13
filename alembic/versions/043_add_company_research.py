"""Add company_research table (feature 1.4 AI Company Research Agent).

Revision ID: 043_company_research
Revises: 042_enrollment_optimization
Create Date: 2026-07-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "043_company_research"
down_revision = "042_enrollment_optimization"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "company_research",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("research_brief", sa.Text, nullable=True),
        sa.Column("pain_points", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("outreach_angles", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("competitive_intel", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("sources_used", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("raw_signals", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("model_used", sa.String(50), nullable=True),
        sa.Column("is_stale", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("researched_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_company_research_company_id", "company_research", ["company_id"])


def downgrade() -> None:
    op.drop_index("ix_company_research_company_id", table_name="company_research")
    op.drop_table("company_research")
