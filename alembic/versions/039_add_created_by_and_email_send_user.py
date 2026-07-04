"""Add users.created_by and email_sends.user_id for auth + communication attribution.

Revision ID: 039_created_by_email_user
Revises: 038_add_user_id_to_activity_tables
Create Date: 2026-07-04
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "039_created_by_email_user"
down_revision = "038_add_user_id_to_activity_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Track which admin created a user account.
    op.add_column(
        "users",
        sa.Column("created_by", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_users_created_by",
        "users",
        "users",
        ["created_by"],
        ["id"],
        ondelete="SET NULL",
    )

    # Attribute each outbound email to the user who sent it.
    op.add_column(
        "email_sends",
        sa.Column("user_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_email_sends_user_id", "email_sends", ["user_id"])
    op.create_foreign_key(
        "fk_email_sends_user_id",
        "email_sends",
        "users",
        ["user_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_email_sends_user_id", "email_sends", type_="foreignkey")
    op.drop_index("ix_email_sends_user_id", table_name="email_sends")
    op.drop_column("email_sends", "user_id")

    op.drop_constraint("fk_users_created_by", "users", type_="foreignkey")
    op.drop_column("users", "created_by")
