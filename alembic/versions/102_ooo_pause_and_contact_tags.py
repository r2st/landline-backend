"""Out-of-office pauses that can be undone, and taggable contacts.

``outreach_enrollments.pause_reason`` — an out-of-office autoresponder now
genuinely pauses the sequence rather than pushing ``next_send_at`` forward while
the enrollment stays ``active`` (which read as live on every dashboard, and was
silently undone by anything that recomputed the date). The reason column is what
makes the automatic resume safe: a sweep looking only for "paused with a due
date" would also revive enrollments paused for a missing email address, a dead
domain, a consent block, or an operator's decision. NULL means "not ours".

``contacts.tags`` — free-form operator labels for the contact management view
(filter, bulk-tag, saved segments). JSONB array of strings, mirroring
``companies.tags``, defaulting to ``[]`` so no read has to handle NULL.

Both columns are nullable/defaulted, so this is a safe online add.

Revision ID: 102_ooo_pause_and_contact_tags
Revises: 101_outreach_events_inbox_index
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "102_ooo_pause_and_contact_tags"
down_revision = "101_outreach_events_inbox_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "outreach_enrollments",
        sa.Column("pause_reason", sa.String(length=30), nullable=True),
    )
    # The resume sweep's exact predicate: paused, ours, and due. Partial so the
    # index stays tiny — the vast majority of enrollments are never OOO-paused.
    op.create_index(
        "ix_enrollments_ooo_resume",
        "outreach_enrollments",
        ["pause_reason", "next_send_at"],
        postgresql_where=sa.text("pause_reason IS NOT NULL"),
    )

    op.add_column(
        "contacts",
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    # Containment queries ("every contact tagged 'trade-show-2026'") are the
    # whole point of the column; GIN is what makes them not a full scan.
    op.create_index(
        "ix_contacts_tags", "contacts", ["tags"], postgresql_using="gin"
    )


def downgrade() -> None:
    op.drop_index("ix_contacts_tags", table_name="contacts")
    op.drop_column("contacts", "tags")
    op.drop_index("ix_enrollments_ooo_resume", table_name="outreach_enrollments")
    op.drop_column("outreach_enrollments", "pause_reason")
