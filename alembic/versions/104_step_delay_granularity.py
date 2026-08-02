"""Give a sequence step a cadence it can actually express.

``sequence_steps`` could only say "N days". Three columns widen that:

* ``delay_hours`` — added to ``delay_days``, so a step can be a same-afternoon
  nudge rather than being rounded up to tomorrow.
* ``delay_business_days`` — count the day portion Mon–Fri. The send window
  already refuses to put mail on the wire at the weekend, so today a 3-day step
  from Thursday and a 2-day step from Friday both land on the same Monday: two
  different intended spacings, one actual one.
* ``delay_jitter_minutes`` — a stable per-enrollment spread, so a batch enrolled
  together stops coming due in one identical block.

All three default to the value that reproduces current behaviour exactly
(0 / false / 0), so every existing step keeps the schedule it has. Safe online
add — three defaulted columns, no rewrite, no index.

Revision ID: 104_step_delay_granularity
Revises: 103_draft_personalization
"""

import sqlalchemy as sa
from alembic import op

revision = "104_step_delay_granularity"
down_revision = "103_draft_personalization"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sequence_steps",
        sa.Column("delay_hours", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "sequence_steps",
        sa.Column(
            "delay_business_days", sa.Boolean(), nullable=False, server_default="false"
        ),
    )
    op.add_column(
        "sequence_steps",
        sa.Column("delay_jitter_minutes", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("sequence_steps", "delay_jitter_minutes")
    op.drop_column("sequence_steps", "delay_business_days")
    op.drop_column("sequence_steps", "delay_hours")
