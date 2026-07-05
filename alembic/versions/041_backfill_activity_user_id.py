"""Backfill user_id on existing activity records.

Project-wide user scoping now filters activity data (tasks, call tasks, call
notifications, follow-ups, interactions, deal notes, email sends) by
``user_id``. Rows created before scoping shipped have it NULL and would become
invisible to everyone except the "All Users" admin view. Assign those legacy
rows to the earliest admin (falling back to the earliest user of any role) so
the original operator keeps seeing their history and it stays reachable. This
is a non-destructive backfill — no data is dropped.

Revision ID: 041_backfill_activity_user_id
Revises: 040_backfill_account_user_id
Create Date: 2026-07-04
"""

from alembic import op

revision = "041_backfill_activity_user_id"
down_revision = "040_backfill_account_user_id"
branch_labels = None
depends_on = None


# Earliest admin first, then earliest user of any role. Postgres sorts boolean
# false < true, so ``(role = 'admin') DESC`` puts admins ahead.
_OWNER_SUBQUERY = (
    "SELECT id FROM users ORDER BY (role = 'admin') DESC, created_at ASC LIMIT 1"
)

_TABLES = (
    "tasks",
    "call_tasks",
    "call_notifications",
    "follow_ups",
    "interactions",
    "deal_notes",
    "email_sends",
)


def upgrade() -> None:
    for table in _TABLES:
        op.execute(
            f"""
            UPDATE {table}
            SET user_id = ({_OWNER_SUBQUERY})
            WHERE user_id IS NULL
              AND EXISTS (SELECT 1 FROM users)
            """
        )


def downgrade() -> None:
    # Non-reversible data backfill — nothing to undo. The columns themselves are
    # dropped by migrations 038/039's downgrades.
    pass
