"""Make a step's trip to the wire a thing the database remembers.

``send_sequence_step`` put a message on the wire and *then* wrote what it had
done — the ``OutreachEvent``, the advanced ``current_step``, the threading ids —
in a transaction that commits afterwards. Everything between the send and that
commit is a window where the mail is already gone and nothing records it: a
dropped connection at ``COMMIT``, a statement timeout, a worker killed
mid-transaction. The handler rolls back and re-raises, Celery retries in sixty
seconds, and the retry finds an enrollment that still says "step 1, active, due"
— because that is exactly what the rollback restored — and sends the same cold
email to the same person again.

The other way in needs no fault at all. The queue sweep runs every five minutes
and marks nothing when it dispatches, so an enrollment whose task is still
sitting in the broker is due on the next sweep too. A worker that takes longer
than five minutes to drain its backlog therefore accumulates copies of every
task in it — six of each if it takes half an hour.

Two columns, two different jobs:

``send_claim_step`` / ``send_claimed_at`` is the correctness one. The send task
claims ``(enrollment, step)`` in its own committed transaction immediately
before the dispatcher call, so the claim survives the rollback of the send it
describes. A conditional UPDATE is the whole mechanism — the row lock makes it
atomic, and rowcount 0 means somebody else already owns this step. It is
deliberately *not* cleared on success: after step 1 ships, the column reads 1,
which is a useful thing to be able to ask ("the last step we put on the wire")
and costs nothing, because claiming step 2 is a different value.

``dispatched_at`` is the cheap one. The sweep stamps it when it queues a task
and skips anything stamped inside the lease, so the duplicate task is never
created rather than created and refused. It expires, so a task lost with its
broker is re-dispatched rather than stranded.

Existing rows read NULL for all three, which is correct: an enrollment nobody
has dispatched has no claim and no lease.

Revision ID: 109_enrollment_send_claim
Revises: 108_drop_cadence_pattern
"""

import sqlalchemy as sa
from alembic import op

revision = "109_enrollment_send_claim"
down_revision = "108_drop_cadence_pattern"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "outreach_enrollments",
        sa.Column("send_claim_step", sa.Integer(), nullable=True),
    )
    op.add_column(
        "outreach_enrollments",
        sa.Column("send_claimed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "outreach_enrollments",
        sa.Column("dispatched_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("outreach_enrollments", "dispatched_at")
    op.drop_column("outreach_enrollments", "send_claimed_at")
    op.drop_column("outreach_enrollments", "send_claim_step")
