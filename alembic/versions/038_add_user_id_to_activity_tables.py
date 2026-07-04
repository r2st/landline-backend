"""Add user_id to tasks, call_tasks, call_notifications, follow_ups, interactions, deal_notes.

Revision ID: 038_add_user_id_to_activity_tables
Revises: 037_add_user_id_to_email_accounts
Create Date: 2026-07-04
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "038_add_user_id_to_activity_tables"
down_revision = "037_add_user_id_to_email_accounts"
branch_labels = None
depends_on = None

_TABLES = [
    "tasks",
    "call_tasks",
    "call_notifications",
    "follow_ups",
    "interactions",
    "deal_notes",
]


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("user_id", UUID(as_uuid=True), nullable=True))
        op.create_index(f"ix_{table}_user_id", table, ["user_id"])
        op.create_foreign_key(
            f"fk_{table}_user_id", table, "users",
            ["user_id"], ["id"], ondelete="SET NULL",
        )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_constraint(f"fk_{table}_user_id", table, type_="foreignkey")
        op.drop_index(f"ix_{table}_user_id", table_name=table)
        op.drop_column(table, "user_id")
