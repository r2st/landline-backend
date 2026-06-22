"""Add deals table (post-conversion revenue opportunities)

Revision ID: 013_add_deals
Revises: 012_add_pipeline_stage
Create Date: 2026-06-22
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "013_add_deals"
down_revision = "012_add_pipeline_stage"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "deals",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id",
            UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("value", sa.Numeric(14, 2), nullable=True),
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("close_date", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('won','lost','pending')", name="ck_deals_status"),
    )
    op.create_index("ix_deals_company_id", "deals", ["company_id"])
    op.create_index("ix_deals_status", "deals", ["status"])


def downgrade():
    op.drop_index("ix_deals_status", table_name="deals")
    op.drop_index("ix_deals_company_id", table_name="deals")
    op.drop_table("deals")
