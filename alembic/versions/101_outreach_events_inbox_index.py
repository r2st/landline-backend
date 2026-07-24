"""Index the inbox's new scan of outreach_events.

The unified inbox now reads sequence sends from ``outreach_events`` — the
sequence engine records a send as ``OutreachEvent(event_type='sent')`` and
writes no ``email_sends`` row, so without this the outbound half of every
Dialtone conversation was missing from the inbox entirely.

That read is "the most recent N sent events for this org", which on the existing
schema (only ``ix_events_enrollment``) means a sequential scan plus a sort on
every inbox page load, growing with the whole send history. This composite
matches the query exactly: org filter, event-type predicate, then the
descending recency order it pages from.

Revision ID: 101_outreach_events_inbox_index
Revises: 100_discovered_business_contact_name
"""

from alembic import op

revision = "101_outreach_events_inbox_index"
down_revision = "100_discovered_business_contact_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_outreach_events_org_type_occurred",
        "outreach_events",
        ["org_id", "event_type", "occurred_at"],
        postgresql_ops={"occurred_at": "DESC"},
    )


def downgrade() -> None:
    op.drop_index("ix_outreach_events_org_type_occurred", table_name="outreach_events")
