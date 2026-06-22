"""SMTP sender accounts for non-OAuth email sending.

Adds the smtp_accounts table: manually-configured senders (Outlook/Office 365,
custom domains, any authenticated SMTP host) that participate in the same
sender pool as Gmail accounts. The SMTP password is encrypted at rest. A partial
unique index enforces "at most one active sender" within this table; the single
primary-sender choice across Gmail + SMTP is coordinated in application code.

Revision ID: 016_smtp_accounts
Revises: 015_extend_interactions
Create Date: 2026-06-22 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "016_smtp_accounts"
down_revision: Union[str, None] = "015_extend_interactions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "smtp_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False, server_default="587"),
        sa.Column("username", sa.String(320), nullable=False),
        sa.Column("password_encrypted", sa.Text(), nullable=False),
        sa.Column("use_tls", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("use_ssl", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("sending_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("status", sa.String(20), nullable=False, server_default="connected"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("daily_send_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("daily_count_reset_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("connected_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_used_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", name="uq_smtp_accounts_email"),
        sa.CheckConstraint(
            "status IN ('connected','error')",
            name="ck_smtp_account_status",
        ),
    )
    # At most one active sender at a time within this table.
    op.create_index(
        "uq_smtp_accounts_single_active",
        "smtp_accounts",
        ["is_active"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    op.drop_index("uq_smtp_accounts_single_active", table_name="smtp_accounts")
    op.drop_table("smtp_accounts")
