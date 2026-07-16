"""Reply classification for Dialtone (Feature 1).

Adds the persisted 5-category reply classifier + priority-routing columns to
``email_replies`` and a hard ``suppressed`` compliance flag (plus a general
``status``) to ``contacts``:

* ``email_replies.reply_classification`` — one of ``interested`` |
  ``not_interested`` | ``out_of_office`` | ``wrong_person`` | ``unsubscribe`` |
  ``unknown``, or ``NULL`` (not yet processed). Indexed for the inbox query.
* ``classification_confidence`` — the classifier's 0..1 confidence.
* ``priority_flag`` — hot-lead routing marker for the priority inbox. Indexed.
* ``classified_at`` — guards re-runs (idempotency).
* ``classification_meta`` — return_date, referral, keyword-hit, model, etc.
* ``handled_action`` — which auto-action was taken (``requeued`` |
  ``unsubscribed`` | ``sequence_removed`` | ``flagged_cleanup`` | ``none``).

* ``contacts.suppressed`` — the single source of truth for "never email again".
  A suppressed contact is filtered out of every send path. Indexed.
* ``contacts.status`` — general contact lifecycle marker; Feature 1 sets it to
  ``wrong_person`` for list-cleanup handoff.

A composite index ``(reply_classification, priority_flag, received_at)`` backs
the newest-first priority-inbox query.

Revision ID: 060_reply_classification
Revises: 059_launch_sender_defaults
Create Date: 2026-07-16
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "060_reply_classification"
down_revision = "059_launch_sender_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- email_replies: persisted classification + routing ---
    op.add_column("email_replies", sa.Column("reply_classification", sa.String(20), nullable=True))
    op.add_column("email_replies", sa.Column("classification_confidence", sa.Float(), nullable=True))
    op.add_column(
        "email_replies",
        sa.Column("priority_flag", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column("email_replies", sa.Column("classified_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column(
        "email_replies",
        sa.Column("classification_meta", postgresql.JSONB, nullable=False, server_default="{}"),
    )
    op.add_column("email_replies", sa.Column("handled_action", sa.String(40), nullable=True))

    op.create_index(
        "ix_email_replies_reply_classification", "email_replies", ["reply_classification"]
    )
    op.create_index("ix_email_replies_priority_flag", "email_replies", ["priority_flag"])
    # Composite index for the priority-inbox query (filter by classification +
    # priority, newest-first).
    op.create_index(
        "ix_email_replies_inbox",
        "email_replies",
        ["reply_classification", "priority_flag", "received_at"],
    )

    # --- contacts: compliance suppression + lifecycle status ---
    op.add_column(
        "contacts",
        sa.Column("suppressed", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column("contacts", sa.Column("status", sa.String(20), nullable=True))
    op.create_index("ix_contacts_suppressed", "contacts", ["suppressed"])


def downgrade() -> None:
    op.drop_index("ix_contacts_suppressed", table_name="contacts")
    op.drop_column("contacts", "status")
    op.drop_column("contacts", "suppressed")

    op.drop_index("ix_email_replies_inbox", table_name="email_replies")
    op.drop_index("ix_email_replies_priority_flag", table_name="email_replies")
    op.drop_index("ix_email_replies_reply_classification", table_name="email_replies")
    op.drop_column("email_replies", "handled_action")
    op.drop_column("email_replies", "classification_meta")
    op.drop_column("email_replies", "classified_at")
    op.drop_column("email_replies", "priority_flag")
    op.drop_column("email_replies", "classification_confidence")
    op.drop_column("email_replies", "reply_classification")
