"""Add optimization_context to outreach_enrollments (feature 1.2 sequence engine).

Revision ID: 042_enrollment_optimization
Revises: 041_backfill_activity_user_id
Create Date: 2026-07-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "042_enrollment_optimization"
down_revision = "041_backfill_activity_user_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "outreach_enrollments",
        sa.Column(
            "optimization_context",
            postgresql.JSONB,
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("outreach_enrollments", "optimization_context")
