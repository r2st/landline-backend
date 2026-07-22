"""Inbox thread state — per-user archive / snooze / read override.

Chains off ``094_sender_health_reputation_nullable``.

Adds the ``inbox_thread_state`` table backing the Inbox improvements
(``docs/INBOX_IMPROVEMENTS.md``): archive/done, snooze, and thread-level read
override — all view state keyed by ``(gmail_thread_id, user_id)`` so it never
touches the shared ``email_sends`` / ``email_replies`` message rows.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "095_inbox_thread_state"
down_revision = "094_sender_health_reputation_nullable"
branch_labels = None
depends_on = None

# The first-party org id (Apprend), used as the server_default for org_id so raw
# inserts are stamped even outside a request context — mirrors OrgScopedMixin.
_APPREND_ORG_ID = "a9910000-0000-4000-8000-000000000001"


def upgrade() -> None:
    op.create_table(
        "inbox_thread_state",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("gmail_thread_id", sa.String(length=255), nullable=False),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "is_archived",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("snoozed_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("read_override", sa.Boolean(), nullable=True),
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
        sa.UniqueConstraint(
            "gmail_thread_id", "user_id", name="uq_inbox_thread_state_thread_user"
        ),
    )
    op.create_index(
        "ix_inbox_thread_state_gmail_thread_id",
        "inbox_thread_state",
        ["gmail_thread_id"],
    )
    op.create_index(
        "ix_inbox_thread_state_user_id", "inbox_thread_state", ["user_id"]
    )
    op.create_index(
        "ix_inbox_thread_state_is_archived", "inbox_thread_state", ["is_archived"]
    )
    op.create_index(
        "ix_inbox_thread_state_org_id", "inbox_thread_state", ["org_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_inbox_thread_state_org_id", table_name="inbox_thread_state")
    op.drop_index("ix_inbox_thread_state_is_archived", table_name="inbox_thread_state")
    op.drop_index("ix_inbox_thread_state_user_id", table_name="inbox_thread_state")
    op.drop_index(
        "ix_inbox_thread_state_gmail_thread_id", table_name="inbox_thread_state"
    )
    op.drop_table("inbox_thread_state")
