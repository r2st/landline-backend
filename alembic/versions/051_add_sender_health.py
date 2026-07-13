"""Add Phase 3.3 sender_health table (deliverability optimizer).

Revision ID: 051_add_sender_health
Revises: 050_add_churn_expansion
Create Date: 2026-07-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "051_add_sender_health"
down_revision = "050_add_churn_expansion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sender_health",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_type", sa.String(10), nullable=False, server_default="gmail"),
        sa.Column("account_email", sa.String(255), nullable=True),
        sa.Column("sent_7d", sa.Integer, nullable=False, server_default="0"),
        sa.Column("bounces_7d", sa.Integer, nullable=False, server_default="0"),
        sa.Column("complaints_7d", sa.Integer, nullable=False, server_default="0"),
        sa.Column("opens_7d", sa.Integer, nullable=False, server_default="0"),
        sa.Column("bounce_rate_7d", sa.Float, nullable=False, server_default="0"),
        sa.Column("complaint_rate_7d", sa.Float, nullable=False, server_default="0"),
        sa.Column("open_rate_7d", sa.Float, nullable=False, server_default="0"),
        sa.Column("reputation_score", sa.Integer, nullable=False, server_default="100"),
        sa.Column("throttle_factor", sa.Float, nullable=False, server_default="1.0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="healthy"),
        sa.Column("details", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("last_assessed", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("account_type", "account_id", name="uq_sender_health_account"),
    )
    op.create_index("ix_sender_health_account_id", "sender_health", ["account_id"])


def downgrade() -> None:
    op.drop_table("sender_health")
