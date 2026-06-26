"""A/B testing and scheduling fields for email sends and sequence status.

Adds ab_variant and scheduled_at columns to email_sends, and a status
column (with check constraint) to outreach_sequences.

Revision ID: 020_ab_testing_and_scheduling
Revises: 019_email_signatures
Create Date: 2026-06-22 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "020_ab_testing_and_scheduling"
down_revision: Union[str, None] = "019_email_signatures"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "email_sends",
        sa.Column("ab_variant", sa.String(1), nullable=True),
    )
    op.add_column(
        "email_sends",
        sa.Column("scheduled_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "outreach_sequences",
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
    )
    op.create_check_constraint(
        "ck_sequence_status",
        "outreach_sequences",
        "status IN ('active','paused','completed')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_sequence_status", "outreach_sequences", type_="check")
    op.drop_column("outreach_sequences", "status")
    op.drop_column("email_sends", "scheduled_at")
    op.drop_column("email_sends", "ab_variant")
