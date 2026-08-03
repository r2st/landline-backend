"""Let a bounce ledger row say whether the funnel already counted it.

``DangerMonitor`` sums three bounce populations, and two of them describe the
same event. ``bounce_ingestion`` writes a ``BounceRecord`` and then calls
``record_bounce``, which books a ``bounced`` OutreachEvent against the
enrollment that sent. Population 2 counts the event; population 3 counted the
ledger row; the sum counted the bounce twice.

That was not always true, which is why it survived. When the ledger was added,
``bounce_handler`` wrote no funnel event at all — the comment calling
BounceRecord "most real bounces" was accurate — and the populations were
genuinely disjoint. They began to overlap the day the funnel write landed, and a
sum has nothing to notice: no error, no failing test, just a number twice its
true size on the one metric whose global breach flips the platform-wide kill
switch. The effective auto-pause threshold became half the configured one, so
outreach halted for every tenant at 2.5% while the console showed 5% and the
settings page showed 5%.

``funnel_events`` is the count of ``bounced`` OutreachEvents the notification
produced. The rail counts ledger rows where it is zero: the bounces no sequence
recorded — manual sends, and addresses whose enrollments had already finished.

A count rather than a boolean because ``contacts.email`` has no unique
constraint, so one address can hold several rows and stop several enrollments.

Existing rows get ``0``, which is the honest default in the only direction that
is safe here: an old row that *did* produce a funnel event stays double-counted
until it ages out of the 24h window, so the rail keeps erring toward pausing
rather than toward missing a breach. Backfilling would mean re-deriving which
DSN produced which event from timestamps and addresses, and inventing that
mapping to make a day of history tidier is a worse trade than a day of the
previous behaviour.

Revision ID: 116_bounce_record_funnel_events
Revises: 115_org_scoped_postmaster_uniques
"""

import sqlalchemy as sa
from alembic import op

revision = "116_bounce_record_funnel_events"
down_revision = "115_org_scoped_postmaster_uniques"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bounce_records",
        sa.Column(
            "funnel_events",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("bounce_records", "funnel_events")
