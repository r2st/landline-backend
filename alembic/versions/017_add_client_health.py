"""Add client_health table (point-in-time client-health snapshots)

Revision ID: 017_add_client_health
Revises: 016_smtp_accounts
Create Date: 2026-06-22
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "017_add_client_health"
down_revision = "016_smtp_accounts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "client_health",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id",
            UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("score", sa.Integer, nullable=False, server_default="0"),
        sa.Column("health_status", sa.String(20), nullable=False),
        sa.Column("last_contact_date", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("response_rate", sa.Float, nullable=False, server_default="0"),
        sa.Column("meeting_frequency", sa.Integer, nullable=False, server_default="0"),
        sa.Column("satisfaction_score", sa.Float, nullable=True),
        sa.Column("recorded_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "health_status IN ('healthy','at_risk','churned')",
            name="ck_client_health_status",
        ),
    )
    op.create_index("ix_client_health_company_id", "client_health", ["company_id"])
    op.create_index("ix_client_health_recorded_at", "client_health", ["recorded_at"])


def downgrade():
    op.drop_index("ix_client_health_recorded_at", table_name="client_health")
    op.drop_index("ix_client_health_company_id", table_name="client_health")
    op.drop_table("client_health")
