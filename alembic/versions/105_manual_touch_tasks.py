"""Tie a manual sequence touch back to the enrollment that asked for it.

A non-email step ("call", "linkedin", "video", "direct_mail") files a
``FollowUp`` for a human and the cadence moves on. Nothing links the two, so
nothing can tell whether the touch was ever made — the sequence advances on the
*filing* of the reminder, and step 3's email goes out two days after a call that
may never have happened.

Three columns on ``follow_ups`` make the link (``enrollment_id`` /
``step_number`` / ``channel``), so completing the task can advance the cadence
from the moment the call happened, and an untouched one can be found and
expired.

``sequence_steps.task_timeout_hours`` is the per-step override on how long to
hold: the default suits a call queue worked over a few days, and a same-afternoon
follow-up needs to say so.

The event-type constraint widens to record what happened to a manual touch —
``task_created`` / ``task_completed`` / ``task_expired``. Deliberately NOT
``sent``: a filed reminder is not a delivered message, and folding it into
``sent`` would put touches nobody made into the denominator of every reply rate
and into the bounce rail's send count.

All added columns are nullable with no default, so existing rows and every
existing query keep behaving exactly as they do.

Revision ID: 105_manual_touch_tasks
Revises: 104_step_delay_granularity
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "105_manual_touch_tasks"
down_revision = "104_step_delay_granularity"
branch_labels = None
depends_on = None

# Kept verbatim so the downgrade can restore exactly what was there.
_OLD_EVENT_TYPES = (
    "'sent','delivered','opened','clicked','replied','bounced','unsubscribed'"
)
_NEW_EVENT_TYPES = (
    _OLD_EVENT_TYPES + ",'task_created','task_completed','task_expired'"
)


def upgrade() -> None:
    op.add_column(
        "follow_ups",
        sa.Column("enrollment_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column("follow_ups", sa.Column("step_number", sa.Integer(), nullable=True))
    op.add_column("follow_ups", sa.Column("channel", sa.String(20), nullable=True))
    op.create_foreign_key(
        "fk_follow_ups_enrollment_id",
        "follow_ups",
        "outreach_enrollments",
        ["enrollment_id"],
        ["id"],
        # CASCADE, not SET NULL: an orphaned "call this contact for step 3" task
        # pointing at a deleted enrollment is a to-do nobody can action or
        # interpret. The reminder exists to serve the cadence and has no meaning
        # once the cadence is gone.
        ondelete="CASCADE",
    )
    # The hold path looks the task up by enrollment on every re-dispatch of a
    # blocked step, which is every queue sweep for as long as the hold lasts.
    op.create_index(
        "ix_follow_ups_enrollment_id", "follow_ups", ["enrollment_id"]
    )

    op.add_column(
        "sequence_steps", sa.Column("task_timeout_hours", sa.Integer(), nullable=True)
    )

    op.drop_constraint("ck_event_type", "outreach_events", type_="check")
    op.create_check_constraint(
        "ck_event_type", "outreach_events", f"event_type IN ({_NEW_EVENT_TYPES})"
    )


def downgrade() -> None:
    # Drop the rows the widened vocabulary allowed before narrowing it, or the
    # constraint cannot be re-created.
    op.execute(
        "DELETE FROM outreach_events WHERE event_type IN "
        "('task_created','task_completed','task_expired')"
    )
    op.drop_constraint("ck_event_type", "outreach_events", type_="check")
    op.create_check_constraint(
        "ck_event_type", "outreach_events", f"event_type IN ({_OLD_EVENT_TYPES})"
    )

    op.drop_column("sequence_steps", "task_timeout_hours")
    op.drop_index("ix_follow_ups_enrollment_id", table_name="follow_ups")
    op.drop_constraint("fk_follow_ups_enrollment_id", "follow_ups", type_="foreignkey")
    op.drop_column("follow_ups", "channel")
    op.drop_column("follow_ups", "step_number")
    op.drop_column("follow_ups", "enrollment_id")
