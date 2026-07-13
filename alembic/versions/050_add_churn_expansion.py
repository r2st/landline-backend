"""Add Phase 3.2 predictive churn + expansion columns to client_health.

Revision ID: 050_add_churn_expansion
Revises: 049_add_prospecting
Create Date: 2026-07-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "050_add_churn_expansion"
down_revision = "049_add_prospecting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("client_health", sa.Column("predicted_churn_probability", sa.Float, nullable=True))
    op.add_column("client_health", sa.Column("churn_risk_factors", postgresql.JSONB, nullable=True))
    op.add_column("client_health", sa.Column("expansion_signals", postgresql.JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("client_health", "expansion_signals")
    op.drop_column("client_health", "churn_risk_factors")
    op.drop_column("client_health", "predicted_churn_probability")
