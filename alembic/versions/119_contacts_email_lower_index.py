"""Index ``lower(contacts.email)`` — the column sixteen queries actually filter on.

``ix_contacts_email`` (migration 001) indexes ``email`` raw. Nothing in this
codebase matches ``email`` raw, and for a good reason stated in
``send_limiter.can_send_to_contact``: ``contacts.email`` has no unique
constraint and is not normalised on write, so one human routinely holds several
rows whose casing differs because that is what the sources supplied. Every
lookup that must be about a *recipient* rather than a *row* therefore compares
``lower(email)`` — and a b-tree on ``email`` cannot serve a predicate on
``lower(email)``. Postgres plans a sequential scan.

Sixteen call sites, and the shape of the cost is not "reports are slow":

* **the compliance gate** — ``suppression.address_blocking_reason``, on the send
  path, once per outbound message;
* **the send limiter's** monthly cap and minimum-gap checks, twice more per
  send;
* **inbound attribution** — ``email_history`` resolving a reply's sender,
  ``tasks.replies``, ``bounce_handler`` and ``complaint_handler``, each on a
  beat sweep over a batch of messages;
* **``gdpr.erase_subject``**, where the scan is the erasure itself.

So the table every one of these scans is the table that grows fastest — the
scraper's output — and the paths that scan it most are the ones that run per
send and per inbound message. It degrades in step with the thing the product
does, which is why it does not look like a problem until it is one.

The index is on the expression, not on a new normalised column: adding one would
need every writer to maintain it, and a writer that forgets is a compliance
lookup that silently misses. An expression index is maintained by the database
and cannot drift from what the queries ask.

``ix_contacts_email`` stays. It is not redundant — a prefix/range query on the
raw column would still use it — and dropping an index is not something to do in
passing on the same migration that adds one.

Revision ID: 119_contacts_email_lower_index
Revises: 118_outcome_event_uniqueness
"""

import sqlalchemy as sa
from alembic import op

revision = "119_contacts_email_lower_index"
down_revision = "118_outcome_event_uniqueness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_contacts_email_lower",
        "contacts",
        [sa.text("lower(email)")],
    )


def downgrade() -> None:
    op.drop_index("ix_contacts_email_lower", table_name="contacts")
