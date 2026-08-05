"""One unsubscribe per sequence step, enforced by the database.

The sibling of 117, for the type that migration left unstated.

117 enumerated which event types genuinely repeat — ``sent`` is written per
attempt and ``send_claim.reconcile`` writes a second one, ``replied`` is
repeatable, the ``task_*`` types record distinct manual touches — and
constrained ``opened`` and ``clicked``. ``unsubscribed`` fell through, and it
does not repeat: a person either objected to a given send or did not.

The writer that makes this matter is ``POST /outreach/events``. It is reachable
by a signed ESP webhook, webhook delivery is at-least-once by design, and that
handler already says so — it answers a duplicate with the row that exists rather
than a 500 an ESP would retry forever. But it recognises a duplicate by catching
the ``IntegrityError`` 117's index raises, so for this type there was nothing to
catch and the redelivery simply inserted again. ``campaign_analytics`` counts
these rows with a plain ``count()``, so one person's opt-out delivered twice is
two opt-outs on every surface reporting ``unsubscribe_rate``, and the rate is
wrong in the direction that makes a campaign look worse than it performed.

**``bounced`` is deliberately not included**, though it fails identically. A
retrying mailer-daemon means duplicate DSNs are an expected fact of mail, so
that rail neutralises them in the *arithmetic* instead:
``outreach_events.count_attempts`` counts distinct ``(enrollment, step)``, which
is what makes a redelivered DSN free rather than a second bounce on the rate
that arms the kill switch. Constraining the table would forbid rows that rail is
built to tolerate and that live databases already hold.

``opt_out.attribute_to_campaign``, the in-application writer, already pre-checks
per contact before writing — but a pre-check is a SELECT followed by an INSERT,
not a constraint, and it runs from public endpoints served concurrently (a
mailbox provider POSTing one-click twice in parallel, a footer click landing
beside that POST). This closes that race rather than changing its intent; the
writer now treats ``IntegrityError`` as "a concurrent pass booked it".

Existing duplicates are deleted before the index is created, keeping the
earliest of each group, exactly as 117 did: that is the rule the application
already applies, so this removes rows that should not have been written rather
than choosing between equals. Ordered by ``(occurred_at, id)`` because rows
written in one race share a reading and tie, and a tie-break that isn't total
leaves the DELETE free to remove both sides.

A separate index rather than a widened 117, so each keeps its own predicate and
its own reason, and so this is reversible on its own.

Revision ID: 118_outcome_event_uniqueness
Revises: 117_engagement_event_uniqueness
"""

import sqlalchemy as sa
from alembic import op

revision = "118_outcome_event_uniqueness"
down_revision = "117_engagement_event_uniqueness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            DELETE FROM outreach_events a
            USING outreach_events b
            WHERE a.event_type = 'unsubscribed'
              AND b.event_type = 'unsubscribed'
              AND b.enrollment_id = a.enrollment_id
              AND b.step_number = a.step_number
              AND (b.occurred_at, b.id) < (a.occurred_at, a.id)
            """
        )
    )
    op.create_index(
        "uq_outreach_events_unsubscribe_once",
        "outreach_events",
        ["enrollment_id", "step_number", "event_type"],
        unique=True,
        postgresql_where=sa.text("event_type = 'unsubscribed'"),
    )


def downgrade() -> None:
    op.drop_index("uq_outreach_events_unsubscribe_once", table_name="outreach_events")
