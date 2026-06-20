"""Gmail OAuth connected accounts for per-user email sending.

Adds the gmail_accounts table: each row is a Google account connected via OAuth
that can send outreach mail as that user (Gmail API), with encrypted refresh /
access tokens. A partial unique index enforces "at most one active sender".

Revision ID: 004_gmail_accounts
Revises: 003_follow_ups
Create Date: 2026-06-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "004_gmail_accounts"
down_revision: Union[str, None] = "003_follow_ups"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "gmail_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("google_sub", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=False),
        sa.Column("access_token_encrypted", sa.Text(), nullable=True),
        sa.Column("token_expiry", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("scopes", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("status", sa.String(20), nullable=False, server_default="connected"),
        sa.Column("connected_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_used_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", name="uq_gmail_accounts_email"),
        sa.UniqueConstraint("google_sub", name="uq_gmail_accounts_google_sub"),
        sa.CheckConstraint(
            "status IN ('connected','revoked','error')",
            name="ck_gmail_account_status",
        ),
    )
    # At most one active sender at a time.
    op.create_index(
        "uq_gmail_accounts_single_active",
        "gmail_accounts",
        ["is_active"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    op.drop_index("uq_gmail_accounts_single_active", table_name="gmail_accounts")
    op.drop_table("gmail_accounts")
