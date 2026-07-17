"""Sender-health snapshot history.

Chains off ``076_phone_enrichment``, the current head.

``sender_health`` keeps only the *current* reputation per sending account, so the
Sender Health dashboard could show today's number but never whether an account is
recovering or sliding. This append-only table stores one dated point per
assessment (reputation, bounce/complaint rate, throttle), giving a real time
series to trend on.

De-normalised on purpose: a historical point must reflect the numbers as they
were, even after the parent ``sender_health`` row moves on or the account is
deleted — so there is no FK back to the account, matching ``sender_health``'s own
deliberate lack of one.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "077_sender_health_snapshot"
down_revision = "076_phone_enrichment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sender_health_snapshot",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_type", sa.String(10), nullable=False, server_default="gmail"),
        sa.Column("account_email", sa.String(255), nullable=True),
        sa.Column("sent_7d", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bounce_rate_7d", sa.Float(), nullable=False, server_default="0"),
        sa.Column("complaint_rate_7d", sa.Float(), nullable=False, server_default="0"),
        sa.Column("open_rate_7d", sa.Float(), nullable=False, server_default="0"),
        sa.Column("reputation_score", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("throttle_factor", sa.Float(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(20), nullable=False, server_default="healthy"),
        sa.Column(
            "captured_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_sender_health_snapshot_account_id", "sender_health_snapshot", ["account_id"],
    )
    op.create_index(
        "ix_sender_health_snapshot_captured_at", "sender_health_snapshot", ["captured_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_sender_health_snapshot_captured_at", table_name="sender_health_snapshot")
    op.drop_index("ix_sender_health_snapshot_account_id", table_name="sender_health_snapshot")
    op.drop_table("sender_health_snapshot")
