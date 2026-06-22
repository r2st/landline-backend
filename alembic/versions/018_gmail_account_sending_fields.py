"""Add sender-rotation columns to gmail_accounts.

The multi-account sender rotation work (and the SMTP feature that followed) added
``sending_enabled``, ``daily_send_count`` and ``daily_count_reset_at`` to the
``GmailAccount`` model and to every dispatch / management query — but no
migration ever added those columns to the ``gmail_accounts`` table. The test
suite builds its schema from the models (``Base.metadata.create_all``), so it
never caught the drift; on a migrated database the columns are missing and every
Gmail account-management call (status, list, set-active, toggle-sending, delete,
dispatch) fails. That left existing Gmail accounts unmanageable and blocked
adding or removing accounts.

This migration backfills the three columns so the deployed schema matches the
model. Columns are added with sensible defaults so existing rows keep working:
already-connected accounts become sending-enabled, matching new-account
behaviour. ``ADD COLUMN IF NOT EXISTS`` keeps it safe on any database that was
hand-patched ahead of this migration.

Revision ID: 018_gmail_account_sending_fields
Revises: 017_add_client_health
Create Date: 2026-06-22 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "018_gmail_account_sending_fields"
down_revision: Union[str, None] = "017_add_client_health"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE gmail_accounts "
        "ADD COLUMN IF NOT EXISTS sending_enabled BOOLEAN NOT NULL DEFAULT true"
    )
    op.execute(
        "ALTER TABLE gmail_accounts "
        "ADD COLUMN IF NOT EXISTS daily_send_count INTEGER NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE gmail_accounts "
        "ADD COLUMN IF NOT EXISTS daily_count_reset_at TIMESTAMP WITH TIME ZONE"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE gmail_accounts DROP COLUMN IF EXISTS daily_count_reset_at")
    op.execute("ALTER TABLE gmail_accounts DROP COLUMN IF EXISTS daily_send_count")
    op.execute("ALTER TABLE gmail_accounts DROP COLUMN IF EXISTS sending_enabled")
