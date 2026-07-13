"""Add deal_predictions table for predictive pipeline forecasting (Feature 1.1).

Revision ID: 045_deal_predictions
Revises: 044_conversation_analysis
Create Date: 2026-07-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "045_deal_predictions"
down_revision = "044_conversation_analysis"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deal_predictions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("deal_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("deals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("close_probability", sa.Float, nullable=False, server_default="0"),
        sa.Column("model_probability", sa.Float, nullable=False, server_default="0"),
        sa.Column("ai_confidence", sa.Float, nullable=True),
        sa.Column("predicted_close_date", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("risk_level", sa.String(20), nullable=False, server_default="on_track"),
        sa.Column("risk_flags", postgresql.JSONB, server_default="[]"),
        sa.Column("contributing_factors", postgresql.JSONB, server_default="{}"),
        sa.Column("ai_summary", sa.Text, nullable=True),
        sa.Column("model_version", sa.String(30), nullable=False, server_default="heuristic_v1"),
        sa.Column("predicted_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "risk_level IN ('on_track','watch','at_risk','stalling')",
            name="ck_deal_predictions_risk_level",
        ),
        sa.CheckConstraint(
            "close_probability BETWEEN 0 AND 100",
            name="ck_deal_predictions_probability",
        ),
    )
    op.create_index("ix_deal_predictions_deal_id", "deal_predictions", ["deal_id"])
    op.create_index("ix_deal_predictions_predicted_at", "deal_predictions", ["predicted_at"])


def downgrade() -> None:
    op.drop_index("ix_deal_predictions_predicted_at", table_name="deal_predictions")
    op.drop_index("ix_deal_predictions_deal_id", table_name="deal_predictions")
    op.drop_table("deal_predictions")
