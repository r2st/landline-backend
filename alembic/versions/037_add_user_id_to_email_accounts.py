"""Add user_id to gmail_accounts and smtp_accounts.

Revision ID: 037_add_user_id_to_email_accounts
Revises: 036_add_users_table
Create Date: 2026-07-04
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "037_add_user_id_to_email_accounts"
down_revision = "036_add_users_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Gmail accounts
    op.add_column("gmail_accounts", sa.Column("user_id", UUID(as_uuid=True), nullable=True))
    op.create_index("ix_gmail_accounts_user_id", "gmail_accounts", ["user_id"])
    op.create_foreign_key(
        "fk_gmail_accounts_user_id", "gmail_accounts", "users",
        ["user_id"], ["id"], ondelete="SET NULL",
    )

    # SMTP accounts
    op.add_column("smtp_accounts", sa.Column("user_id", UUID(as_uuid=True), nullable=True))
    op.create_index("ix_smtp_accounts_user_id", "smtp_accounts", ["user_id"])
    op.create_foreign_key(
        "fk_smtp_accounts_user_id", "smtp_accounts", "users",
        ["user_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_smtp_accounts_user_id", "smtp_accounts", type_="foreignkey")
    op.drop_index("ix_smtp_accounts_user_id", table_name="smtp_accounts")
    op.drop_column("smtp_accounts", "user_id")

    op.drop_constraint("fk_gmail_accounts_user_id", "gmail_accounts", type_="foreignkey")
    op.drop_index("ix_gmail_accounts_user_id", table_name="gmail_accounts")
    op.drop_column("gmail_accounts", "user_id")
