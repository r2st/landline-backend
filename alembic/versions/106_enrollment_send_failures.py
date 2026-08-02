"""Track repeated send failures on an enrollment so they stop being invisible.

``send_sequence_step`` retried three times on an exception and then let the task
die, leaving the enrollment untouched — ``active``, ``next_send_at`` still in the
past. The queue sweep found it due five minutes later and dispatched it again,
forever, for any fault that does not fix itself.

Three columns make that state visible and bounded: how many consecutive
incidents (``consecutive_failures``), what went wrong last (``last_error``) and
when (``last_error_at``). Repeated failures back ``next_send_at`` off
exponentially, and at the configured threshold the enrollment pauses with
``pause_reason='send_failures'`` — outside both automatic resume paths (the OOO
sweep matches its own reason, the kill switch matches ``kill_switch_paused``),
because the fault behind a quarantine does not clear on a timer.

``consecutive_failures`` defaults to 0 server-side so existing rows read as
healthy, which they are — an enrollment with no recorded failure has none.

Revision ID: 106_enrollment_send_failures
Revises: 105_manual_touch_tasks
"""

import sqlalchemy as sa
from alembic import op

revision = "106_enrollment_send_failures"
down_revision = "105_manual_touch_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "outreach_enrollments",
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "outreach_enrollments", sa.Column("last_error", sa.Text(), nullable=True)
    )
    op.add_column(
        "outreach_enrollments",
        sa.Column("last_error_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    # The failures view asks for "enrollments currently failing", which is a
    # small set inside a table that is mostly zeroes. Partial so the index stays
    # proportional to the problem rather than to the table.
    op.create_index(
        "ix_outreach_enrollments_failing",
        "outreach_enrollments",
        ["consecutive_failures"],
        postgresql_where=sa.text("consecutive_failures > 0"),
    )


def downgrade() -> None:
    # Enrollments quarantined by this feature would otherwise stay paused with a
    # reason nothing in the older code understands — and nothing would ever
    # resume them. Hand them back as active.
    op.execute(
        "UPDATE outreach_enrollments SET status = 'active', pause_reason = NULL "
        "WHERE pause_reason = 'send_failures'"
    )
    op.drop_index("ix_outreach_enrollments_failing", table_name="outreach_enrollments")
    op.drop_column("outreach_enrollments", "last_error_at")
    op.drop_column("outreach_enrollments", "last_error")
    op.drop_column("outreach_enrollments", "consecutive_failures")
