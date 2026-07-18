"""Adaptive step delays: per-enrollment delay multiplier.

Chains off ``082_suppression_entries``, the current head.

Adds ``adaptive_delay_factor`` to ``outreach_enrollments`` (Feature 3). The
sequence executor multiplies each step's base ``delay_days`` by this factor when
scheduling the next send; engagement shortens it, silence extends it. Defaults to
1.0 so existing enrollments keep their static cadence until an event moves them.
"""

from alembic import op
import sqlalchemy as sa

revision = "083_adaptive_delay_factor"
down_revision = "082_suppression_entries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "outreach_enrollments",
        sa.Column(
            "adaptive_delay_factor", sa.Float(),
            nullable=False, server_default="1.0",
        ),
    )


def downgrade() -> None:
    op.drop_column("outreach_enrollments", "adaptive_delay_factor")
