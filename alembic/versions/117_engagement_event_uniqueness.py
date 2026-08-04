"""One open and one click per sequence step, enforced by the database.

``routers.tracking._record_outreach_engagement`` deduplicates ``opened`` and
``clicked`` events per ``(enrollment, step)``: a mail client re-fetches the
tracking pixel on every re-render, and the send-time optimizer and every
engagement rate read these as *counts*, so a row per fetch inflates them.

It deduplicates with a SELECT followed by an INSERT, which is not a constraint.
The pixel endpoint is public, unauthenticated and served concurrently — a phone
and a desktop client rendering the same message, a provider prefetch landing
beside the human's open, or the click handler recording the implied ``opened``
while the pixel records the real one. Both requests read no row and both wrote
one. The duplicate is not merely a count: ``adaptive_delay.register_engagement``
runs per recorded event, so a doubled open shortened the enrollment's cadence
twice and pulled the next step in further than the signal justified.

Partial, because it is only true of these two types. ``sent`` is written per
attempt and ``send_claim.reconcile`` writes a second one for an attempt whose
bookkeeping was lost; ``replied`` is genuinely repeatable; the three ``task_*``
types record distinct manual touches.

Existing duplicates are deleted before the index is created, keeping the
earliest of each group — which is the rule the application already applies
("first occurrence wins; repeats are dropped"), so this removes rows that should
never have been written rather than choosing between equals. Ordered by
``(occurred_at, id)`` rather than ``occurred_at`` alone: rows written in the same
race share a reading and tie, and a tie-break that isn't total leaves the DELETE
free to remove both sides.

Revision ID: 117_engagement_event_uniqueness
Revises: 116_bounce_record_funnel_events
"""

import sqlalchemy as sa
from alembic import op

revision = "117_engagement_event_uniqueness"
down_revision = "116_bounce_record_funnel_events"
branch_labels = None
depends_on = None

_ENGAGEMENT_TYPES = "('opened','clicked')"


def upgrade() -> None:
    op.execute(
        sa.text(
            f"""
            DELETE FROM outreach_events a
            USING outreach_events b
            WHERE a.event_type IN {_ENGAGEMENT_TYPES}
              AND b.event_type = a.event_type
              AND b.enrollment_id = a.enrollment_id
              AND b.step_number = a.step_number
              AND (b.occurred_at, b.id) < (a.occurred_at, a.id)
            """
        )
    )
    op.create_index(
        "uq_outreach_events_engagement_once",
        "outreach_events",
        ["enrollment_id", "step_number", "event_type"],
        unique=True,
        postgresql_where=sa.text(f"event_type IN {_ENGAGEMENT_TYPES}"),
    )


def downgrade() -> None:
    op.drop_index("uq_outreach_events_engagement_once", table_name="outreach_events")
