"""Inbox labels — user-created tags for organizing mail threads.

Chains off ``097_api_keys``.

Adds two side tables backing the Inbox labels feature (see
``app/models/inbox_label.py``): ``inbox_labels`` (a user's named, coloured
label) and ``inbox_thread_labels`` (the join attaching a label to a
``gmail_thread_id`` for that user). Both are keyed to the human, like
``inbox_thread_state``, so they never touch the shared message rows.

Snooze needs no schema change: the ``snoozed_until`` column already shipped on
``inbox_thread_state`` in migration 095.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "098_inbox_labels"
down_revision = "097_api_keys"
branch_labels = None
depends_on = None

# Apprend's first-party org id — server_default for org_id so raw inserts are
# stamped outside a request context (mirrors OrgScopedMixin).
_APPREND_ORG_ID = "a9910000-0000-4000-8000-000000000001"


def upgrade() -> None:
    op.create_table(
        "inbox_labels",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column(
            "color", sa.String(length=24), nullable=False, server_default="gray"
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "org_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            server_default=_APPREND_ORG_ID,
        ),
        sa.UniqueConstraint("user_id", "name", name="uq_inbox_label_user_name"),
    )
    op.create_index("ix_inbox_labels_user_id", "inbox_labels", ["user_id"])
    op.create_index("ix_inbox_labels_org_id", "inbox_labels", ["org_id"])

    op.create_table(
        "inbox_thread_labels",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("gmail_thread_id", sa.String(length=255), nullable=False),
        sa.Column(
            "label_id",
            UUID(as_uuid=True),
            sa.ForeignKey("inbox_labels.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "org_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            server_default=_APPREND_ORG_ID,
        ),
        sa.UniqueConstraint(
            "gmail_thread_id",
            "label_id",
            "user_id",
            name="uq_inbox_thread_label_thread_label_user",
        ),
    )
    op.create_index(
        "ix_inbox_thread_labels_gmail_thread_id",
        "inbox_thread_labels",
        ["gmail_thread_id"],
    )
    op.create_index(
        "ix_inbox_thread_labels_label_id", "inbox_thread_labels", ["label_id"]
    )
    op.create_index(
        "ix_inbox_thread_labels_user_id", "inbox_thread_labels", ["user_id"]
    )
    op.create_index(
        "ix_inbox_thread_labels_org_id", "inbox_thread_labels", ["org_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_inbox_thread_labels_org_id", table_name="inbox_thread_labels"
    )
    op.drop_index(
        "ix_inbox_thread_labels_user_id", table_name="inbox_thread_labels"
    )
    op.drop_index(
        "ix_inbox_thread_labels_label_id", table_name="inbox_thread_labels"
    )
    op.drop_index(
        "ix_inbox_thread_labels_gmail_thread_id", table_name="inbox_thread_labels"
    )
    op.drop_table("inbox_thread_labels")

    op.drop_index("ix_inbox_labels_org_id", table_name="inbox_labels")
    op.drop_index("ix_inbox_labels_user_id", table_name="inbox_labels")
    op.drop_table("inbox_labels")
