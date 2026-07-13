"""Lead prioritization & routing: extend lead_scores, add rep_profiles (Feature 1.3).

Revision ID: 046_lead_prioritization
Revises: 045_deal_predictions
Create Date: 2026-07-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "046_lead_prioritization"
down_revision = "045_deal_predictions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- Extend lead_scores with propensity / routing fields. ---
    op.add_column("lead_scores", sa.Column("propensity_score", sa.Integer, nullable=False, server_default="0"))
    op.add_column("lead_scores", sa.Column("signal_velocity", sa.Float, nullable=False, server_default="0"))
    op.add_column("lead_scores", sa.Column("last_signal_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column(
        "lead_scores",
        sa.Column(
            "recommended_rep_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # --- Per-rep expertise / win-rate / capacity profile. ---
    op.create_table(
        "rep_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("industries", postgresql.ARRAY(sa.Text), server_default="{}"),
        sa.Column("win_rate_by_segment", postgresql.JSONB, server_default="{}"),
        sa.Column("total_won", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_lost", sa.Integer, nullable=False, server_default="0"),
        sa.Column("overall_win_rate", sa.Float, nullable=False, server_default="0"),
        sa.Column("avg_deal_velocity_days", sa.Float, nullable=True),
        sa.Column("max_capacity", sa.Integer, nullable=False, server_default="25"),
        sa.Column("active_load", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", name="uq_rep_profiles_user_id"),
    )
    op.create_index("ix_rep_profiles_user_id", "rep_profiles", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_rep_profiles_user_id", table_name="rep_profiles")
    op.drop_table("rep_profiles")
    op.drop_column("lead_scores", "recommended_rep_id")
    op.drop_column("lead_scores", "last_signal_at")
    op.drop_column("lead_scores", "signal_velocity")
    op.drop_column("lead_scores", "propensity_score")
