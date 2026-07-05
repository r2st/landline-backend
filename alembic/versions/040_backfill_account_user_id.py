"""Backfill user_id on existing gmail/smtp accounts.

Migrations 037 added a nullable ``user_id`` to ``gmail_accounts`` and
``smtp_accounts`` but existing rows (connected before auth shipped) have it
NULL. With user-scoped reads now enforced, those unowned accounts would be
invisible to everyone. Assign them to the earliest admin user (falling back to
the earliest user of any role) so the original operator keeps seeing their
inbox and connected accounts. The account owner can reconnect to re-claim if
this assignment is wrong.

Revision ID: 040_backfill_account_user_id
Revises: 039_created_by_email_user
Create Date: 2026-07-04
"""

from alembic import op

revision = "040_backfill_account_user_id"
down_revision = "039_created_by_email_user"
branch_labels = None
depends_on = None


# Earliest admin first, then earliest user of any role. Postgres sorts
# boolean false < true, so ``(role = 'admin') DESC`` puts admins ahead.
_OWNER_SUBQUERY = (
    "SELECT id FROM users ORDER BY (role = 'admin') DESC, created_at ASC LIMIT 1"
)


def _backfill(table: str) -> None:
    op.execute(
        f"""
        UPDATE {table}
        SET user_id = ({_OWNER_SUBQUERY})
        WHERE user_id IS NULL
          AND EXISTS (SELECT 1 FROM users)
        """
    )


def upgrade() -> None:
    _backfill("gmail_accounts")
    _backfill("smtp_accounts")


def downgrade() -> None:
    # Non-reversible data backfill — nothing to undo. The column itself is
    # dropped by migration 037's downgrade.
    pass
