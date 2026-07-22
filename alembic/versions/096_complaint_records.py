"""Complaint / feedback-loop (FBL) ingestion ledger (P1 fix).

Chains off ``095_inbox_thread_state``.

Adds ``complaint_records`` — one row per ingested abuse/complaint report,
deduped on ``gmail_message_id``. Mirrors ``bounce_records`` (migration 070) but
for spam complaints: a complained-about recipient is suppressed immediately, the
same path as a hard bounce. ``org_id`` is declared here directly (this table is
created after the multi-tenancy retrofit in 078, so it is org-scoped from birth).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "096_complaint_records"
down_revision = "095_inbox_thread_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "complaint_records",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        # The dedup key. UNIQUE is load-bearing: it is what stops a repeated sweep
        # from re-suppressing / re-auditing the same complaint.
        sa.Column("gmail_message_id", sa.String(255), nullable=False),
        sa.Column(
            "gmail_account_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("gmail_accounts.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("recipient_email", sa.String(320), nullable=False),
        sa.Column("feedback_type", sa.String(20), nullable=False, server_default="abuse"),
        sa.Column("source", sa.String(255), nullable=True),
        sa.Column("diagnostic", sa.Text(), nullable=True),
        sa.Column(
            "result", postgresql.JSONB(astext_type=sa.Text()),
            nullable=False, server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "processed_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_complaint_records_gmail_message_id", "complaint_records",
        ["gmail_message_id"], unique=True,
    )
    op.create_index(
        "ix_complaint_records_recipient_email", "complaint_records", ["recipient_email"],
    )
    op.create_index(
        "ix_complaint_records_org_id", "complaint_records", ["org_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_complaint_records_org_id", table_name="complaint_records")
    op.drop_index("ix_complaint_records_recipient_email", table_name="complaint_records")
    op.drop_index("ix_complaint_records_gmail_message_id", table_name="complaint_records")
    op.drop_table("complaint_records")
